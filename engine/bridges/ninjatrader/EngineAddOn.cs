// EngineAddOn.cs - NinjaTrader 8 chartless AddOn bridging NT8 <-> Python engine.
// One market socket + one broker socket; every line tagged with "symbol".
// Instruments are DYNAMIC: engine sends {"t":"sub","symbol":"X"} on broker socket.
// Written against decompiled NT8 8.1.8.3 API (ilspycmd) - no invented signatures.
using System;
using System.Collections.Generic;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;

namespace NinjaTrader.NinjaScript
{
    public class EngineAddOn : AddOnBase
    {
        private TcpListener marketListener, brokerListener;
        private readonly EngineBridge.RelayChannel marketCh = new EngineBridge.RelayChannel("market", 50000);
        private readonly EngineBridge.RelayChannel brokerCh = new EngineBridge.RelayChannel("broker", 5000);
        private Account account;
        private readonly Dictionary<string, Order> live = new Dictionary<string, Order>();
        private readonly Dictionary<string, double> lastBid = new Dictionary<string, double>();
        private readonly Dictionary<string, double> lastAsk = new Dictionary<string, double>();

        [NinjaScriptProperty]
        [Display(Name = "MarketPort", GroupName = "Engine", Order = 1)]
        public int MarketPort { get; set; }
        [NinjaScriptProperty]
        [Display(Name = "BrokerPort", GroupName = "Engine", Order = 2)]
        public int BrokerPort { get; set; }
        [NinjaScriptProperty]
        [Display(Name = "Account", GroupName = "Engine", Order = 3)]
        public string AccountName { get; set; }

        private class Sub
        {
            public string Symbol;
            public Instrument Instrument;
            public List<string> HistBars = new List<string>();
        }
        private readonly List<Sub> subs = new List<Sub>();
        private readonly object subsLock = new object();
        private static readonly DateTime Epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
        private static readonly string LogPath = System.IO.Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
            "NinjaTrader 8", "engine_addon.log");
        private void RLog(string msg)
        {
            try { System.IO.File.AppendAllText(LogPath, DateTime.Now.ToString("HH:mm:ss.fff") + "  " + msg + "\n"); }
            catch { }
        }

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "EngineAddOn";
                MarketPort = 37301;
                BrokerPort = 37302;
                AccountName = "Sim101";
            }
            else if (State == State.Configure)
            {
                try
                {
                    StartServer(ref marketListener, MarketPort, marketCh, false);
                    StartServer(ref brokerListener, BrokerPort, brokerCh, true);
                }
                catch (Exception ex)
                {
                    RLog("FAILED to start servers: " + ex.Message);
                    throw;
                }
                account = NinjaTrader.Cbi.Account.All.FirstOrDefault(a => string.Equals(a.Name, AccountName, StringComparison.OrdinalIgnoreCase));
                if (account == null) account = NinjaTrader.Cbi.Account.All.FirstOrDefault();
                if (account != null)
                {
                    account.ExecutionUpdate += OnAccountExecution;
                    account.PositionUpdate += OnAccountPosition;
                    account.OrderUpdate += OnAccountOrder;
                }
                RLog("=== EngineAddOn configured. market:" + MarketPort + " broker:" + BrokerPort +
                     " account=" + (account != null ? account.Name : "NONE") + " ===");
            }
            else if (State == State.Terminated)
            {
                try
                {
                    if (account != null)
                    {
                        account.ExecutionUpdate -= OnAccountExecution;
                        account.PositionUpdate -= OnAccountPosition;
                        account.OrderUpdate -= OnAccountOrder;
                    }
                    if (marketListener != null) marketListener.Stop();
                    if (brokerListener != null) brokerListener.Stop();
                    marketCh.Dispose();
                    brokerCh.Dispose();
                }
                catch { }
            }
        }

        private void Subscribe(string symbol)
        {
            try
            {
                Instrument inst = Instrument.GetInstrument(symbol);
                // A bare master symbol either cannot be resolved (NQ -> null)
                // or resolves to a live master that OWNS no 1m bar history
                // (ES -> works, but the date-anchored BarsRequest returns 0).
                // In both cases the real minute history lives on the FRONT
                // contract, the object `{symbol} 12-26` names (measured:
                // NQ 12-26 backfilled 13800 1m bars while bare ES returned 0).
                // Request the bars from that contract while keeping the lane on
                // the bare `symbol`: FindSub routes live trades through
                // MasterInstrument.Name, so the bare-name lane still receives
                // realtime data regardless of which object we subscribe on.
                bool bareMaster = inst != null && inst.MasterInstrument != null
                    && string.Equals(symbol, inst.MasterInstrument.Name, StringComparison.OrdinalIgnoreCase);
                Instrument reqInst = inst;
                if (inst == null || bareMaster)
                {
                    string contractSymbol = symbol + " 12-26";
                    Instrument front = Instrument.GetInstrument(contractSymbol);
                    if (front != null)
                    {
                        reqInst = front;
                        RLog("Resolved contract: " + contractSymbol +
                             (bareMaster ? " (master->front for history)" : ""));
                    }
                }
                if (reqInst == null) { RLog("subscribe: NO INSTRUMENT for '" + symbol + "'"); return; }
                lock (subsLock)
                {
                    if (subs.Any(s => string.Equals(s.Symbol, symbol, StringComparison.OrdinalIgnoreCase))) return;
                }
                var sub = new Sub { Symbol = symbol, Instrument = reqInst };
                lock (subsLock) subs.Add(sub);
                // historical 1m bars for warmup backfill. Date-anchored request
                // covering BackfillDays of calendar time (≈ that many RTH 1m
                // bars per session + weekends + overnight), which reliably covers
                // multiple sessions (Oct-08/09 in the NT8 .ncd archive) so zone
                // detectors and the 1d/1h detectors warm on real sessions rather
                // than the previous single 10-bar request. Count-based
                // {@code new BarsRequest(inst, N)} was avoided because on a
                // closed market NT8 can refuse/anchor count-based requests for
                // this subscription type; a date-anchored request is exact and
                // deterministic about which days arrive. NT8's natural backfill
                // max is ~5,100 bars, in the same order as this window.
                const int BackfillDays = 14;
                DateTime fromLocal = DateTime.Now.AddDays(-BackfillDays);
                var req = new BarsRequest(reqInst, fromLocal, DateTime.Now)
                { BarsPeriod = new BarsPeriod { BarsPeriodType = BarsPeriodType.Minute, Value = 1 } };
                req.Request((bars, err, msg) =>
                {
                    if (err != ErrorCode.NoError || bars == null || bars.Bars == null) return;
                    List<string> filled;
                    lock (sub.HistBars)
                    {
                        sub.HistBars.Clear();
                        var bs = bars.Bars;
                        for (int i = 0; i < Math.Min(bs.Count, 20000); i++)
                            sub.HistBars.Add(EncodeBar(bs.GetTime(i), bs.GetOpen(i), bs.GetHigh(i),
                                bs.GetLow(i), bs.GetClose(i), bs.GetVolume(i), sub.Symbol));
                        filled = new List<string>(sub.HistBars);
                    }
                    RLog(sub.Symbol + ": backfill " + sub.HistBars.Count + " 1m bars");
                    // The accept-time SendBackfill() runs BEFORE this async
                    // callback fills HistBars (~1s), so it always sent zero.
                    // THIS is the delivery point: publish each historical bar
                    // as a `bar` message on the shared market channel, reaching
                    // every connected market client (the engine demux routes by
                    // the bar's symbol). Done once per request, so a later
                    // reconnect (SendBackfill at accept) does not double-send
                    // to a client that was connected the whole time.
                    foreach (string b in filled)
                        marketCh.Publish(b);
                });
                // realtime last-trade ticks
                reqInst.MarketData.Update += OnMarketData;
                RLog("subscribed " + symbol);
            }
            catch (Exception ex)
            {
                RLog("subscribe '" + symbol + "' FAILED: " + ex.Message);
            }
        }

        private Sub FindSub(Instrument inst)
        {
            if (inst == null || inst.MasterInstrument == null) return null;
            return FindSub(inst.MasterInstrument.Name);
        }
        private Sub FindSub(string symbol)
        {
            if (symbol == null) return null;
            lock (subsLock)
            {
                foreach (Sub s in subs)
                    if (string.Equals(s.Symbol, symbol, StringComparison.OrdinalIgnoreCase)) return s;
            }
            return null;
        }

        private void OnMarketData(object sender, MarketDataEventArgs e)
        {
            if (e == null || e.Instrument == null) return;
            Sub sub = FindSub(e.Instrument);
            if (sub == null) return;
            long ns = ToNs(e.Time);
            if (e.MarketDataType == MarketDataType.Bid) lastBid[sub.Symbol] = e.Price;
            else if (e.MarketDataType == MarketDataType.Ask) lastAsk[sub.Symbol] = e.Price;
            else if (e.MarketDataType == MarketDataType.Last)
            {
                double bid = lastBid.ContainsKey(sub.Symbol) ? lastBid[sub.Symbol] : 0;
                double ask = lastAsk.ContainsKey(sub.Symbol) ? lastAsk[sub.Symbol] : 0;
                int aggressor = e.Price >= ask && ask > 0 ? 1 : (e.Price <= bid && bid > 0 ? -1 : 1);
                marketCh.Publish(Quote("trade", sub.Symbol, ns, "price", e.Price,
                    "size", e.Volume, "aggressor", aggressor));
            }
        }

        private void OnAccountExecution(object sender, ExecutionEventArgs e)
        {
            if (e == null || e.Execution == null || e.Execution.Instrument == null) return;
            string sym = e.Execution.Instrument.MasterInstrument.Name;
            int qty = e.Execution.Quantity;
            int signed = e.Execution.MarketPosition == MarketPosition.Long ? qty : -qty;
            string name = e.Execution.Order != null ? e.Execution.Order.Name : "";
            brokerCh.Publish("{\"t\":\"fill\",\"ts\":" + ToNs(e.Execution.Time) +
                ",\"order_id\":\"" + name + "\",\"symbol\":\"" + sym +
                "\",\"price\":" + J(e.Execution.Price) + ",\"size\":" + signed +
                ",\"commission\":" + J(e.Execution.Commission) +
                ",\"tag\":\"" + name + "\"}");
        }

        private void OnAccountPosition(object sender, PositionEventArgs e)
        {
            if (e == null || e.Position == null || e.Position.Instrument == null) return;
            string sym = e.Position.Instrument.MasterInstrument.Name;
            int signed = e.Position.MarketPosition == MarketPosition.Long ? e.Position.Quantity
                       : (e.Position.MarketPosition == MarketPosition.Short ? -e.Position.Quantity : 0);
            if (e.Operation == Operation.Remove) signed = 0;
            brokerCh.Publish("{\"t\":\"position\",\"ts\":" + ToNs(DateTime.UtcNow) +
                ",\"symbol\":\"" + sym + "\",\"qty\":" + signed +
                ",\"avg_px\":" + J(e.Position.AveragePrice) + "}");
        }

        private void OnAccountOrder(object sender, OrderEventArgs e)
        {
            if (e == null || e.Order == null || e.Order.Instrument == null) return;
            if (e.Order.OrderState == OrderState.Rejected || e.Order.OrderState == OrderState.Cancelled)
            {
                string sym = e.Order.Instrument.MasterInstrument != null
                    ? e.Order.Instrument.MasterInstrument.Name : e.Order.Instrument.FullName;
                RLog("ORDER " + e.Order.OrderState + ": " + e.Order.Name + " on " +
                     e.Order.Instrument.FullName + " (master " + sym + ")" +
                     " qty=" + e.Order.Quantity + " type=" + e.Order.OrderType);
            }
        }

        private void HandleOrderLine(string line)
        {
            if (account == null) return;
            var m = MiniJson.Parse(line);
            string t = m.Get("t");
            string sym = m.Get("symbol");
            if (t == "sub")
            {
                if (sym.Length > 0 && FindSub(sym) == null)
                    Subscribe(sym);
                return;
            }
            if (sym.Length == 0) return;
            if (t == "place")
            {
                Sub sub = FindSub(sym);
                Instrument inst = sub != null ? sub.Instrument : Instrument.GetInstrument(sym);
                if (inst == null) { RLog("place " + m.Get("order_id") + ": NO INSTRUMENT for '" + sym + "'"); return; }
                RLog("place " + m.Get("order_id") + " on " + inst.FullName +
                     " (master " + (inst.MasterInstrument != null ? inst.MasterInstrument.Name : "?") + ")" +
                     " qty=" + (int)m.Num("qty"));
                try
                {
                    OrderAction action = (int)m.Num("side") > 0 ? OrderAction.Buy : OrderAction.Sell;
                    OrderType type = m.Get("otype") == "LIMIT" ? OrderType.Limit
                                   : m.Get("otype") == "STOP" ? OrderType.StopMarket : OrderType.Market;
                    double limit = type == OrderType.Limit ? m.Num("limit") : 0;
                    double stop = type == OrderType.StopMarket ? m.Num("stop") : 0;
                    Order o = account.CreateOrder(inst, action, type, OrderEntry.Manual,
                        TimeInForce.Day, (int)m.Num("qty"), limit, stop, string.Empty,
                        m.Get("order_id"), Core.Globals.MaxDate, null);
                    lock (live) live[m.Get("order_id")] = o;
                    account.Submit(new[] { o });
                    RLog("submitted OK: " + m.Get("order_id") + " on " + inst.FullName);
                }
                catch (Exception ex)
                {
                    RLog("SUBMIT FAILED for " + m.Get("order_id") + " on " + inst.FullName + ": " +
                         ex.GetType().Name + ": " + ex.Message);
                }
            }
            else if (t == "cancel")
            {
                Order o;
                if (TryGetLive(m.Get("order_id"), out o)) account.Cancel(new[] { o });
            }
            else if (t == "modify")
            {
                Order o;
                if (TryGetLive(m.Get("order_id"), out o))
                {
                    if (m.Num("limit") > 0) o.LimitPriceChanged = m.Num("limit");
                    if (m.Num("stop") > 0) o.StopPriceChanged = m.Num("stop");
                    if (m.Num("qty") > 0) o.QuantityChanged = (int)m.Num("qty");
                    account.Change(new[] { o });
                }
            }
        }

        private bool TryGetLive(string id, out Order o)
        {
            lock (live) return live.TryGetValue(id, out o);
        }

        private void StartServer(ref TcpListener listener, int port,
                                 EngineBridge.RelayChannel ch, bool isBroker)
        {
            listener = new TcpListener(IPAddress.Loopback, port);
            listener.Start();
            var l = listener;
            new Thread(() =>
            {
                while (true)
                {
                    TcpClient c;
                    try { c = l.AcceptTcpClient(); } catch { break; }
                    if (!isBroker) SendBackfill(c);
                    ch.AddClient(c);
                    if (isBroker) new Thread(() => ReadBroker(c)).Start();
                }
            }) { IsBackground = true }.Start();
        }

        private void SendBackfill(TcpClient c)
        {
            try
            {
                var s = c.GetStream();
                var sb = new StringBuilder();
                lock (subsLock)
                {
                    foreach (Sub sub in subs)
                        lock (sub.HistBars)
                            for (int i = 0; i < sub.HistBars.Count; i++)
                                sb.Append(sub.HistBars[i]).Append('\n');
                }
                byte[] bytes = Encoding.UTF8.GetBytes(sb.ToString());
                if (bytes.Length > 0) s.Write(bytes, 0, bytes.Length);
            }
            catch { }
        }

        private void ReadBroker(TcpClient c)
        {
            var buf = new StringBuilder();
            var tmp = new byte[4096];
            try
            {
                var s = c.GetStream();
                int n;
                while ((n = s.Read(tmp, 0, tmp.Length)) > 0)
                {
                    buf.Append(Encoding.UTF8.GetString(tmp, 0, n));
                    int nl;
                    while ((nl = buf.ToString().IndexOf('\n')) >= 0)
                    {
                        string line = buf.ToString(0, nl).Trim();
                        buf.Remove(0, nl + 1);
                        if (line.Length > 0)
                            HandleOrderLine(line);
                    }
                }
            }
            catch { }
        }

        private static long ToNs(DateTime t)
        {
            return (long)((t.ToUniversalTime() - Epoch).Ticks) * 100L;
        }

        private static string J(double d) { return d.ToString("R", CultureInfo.InvariantCulture); }

        private static string Quote(string t, string sym, long ts, params object[] kv)
        {
            var sb = new StringBuilder();
            sb.Append("{\"t\":\"").Append(t).Append("\",\"ts\":").Append(ts)
              .Append(",\"symbol\":\"").Append(sym).Append("\"");
            for (int i = 0; i + 1 < kv.Length; i += 2)
            {
                sb.Append(",\"").Append((string)kv[i]).Append("\":");
                object v = kv[i + 1];
                if (v is string) sb.Append('"').Append((string)v).Append('"');
                else sb.Append(J(Convert.ToDouble(v, CultureInfo.InvariantCulture)));
            }
            sb.Append('}');
            return sb.ToString();
        }

        private static string EncodeBar(DateTime time, double o, double h, double l, double c, long v, string sym)
        {
            return "{\"t\":\"bar\",\"ts\":" + ToNs(time) + ",\"tf\":\"1m\",\"o\":" + J(o) +
                   ",\"h\":" + J(h) + ",\"l\":" + J(l) + ",\"c\":" + J(c) + ",\"v\":" + v +
                   ",\"symbol\":\"" + sym + "\"}";
        }

        public class MiniJson
        {
            private readonly Dictionary<string, string> kv = new Dictionary<string, string>();

            public static MiniJson Parse(string s)
            {
                var j = new MiniJson();
                s = s.Trim();
                if (s.StartsWith("{")) s = s.Substring(1);
                if (s.EndsWith("}")) s = s.Substring(0, s.Length - 1);
                int i = 0, n = s.Length;
                while (i < n)
                {
                    while (i < n && s[i] != '"') i++;
                    if (i >= n) break;
                    i++; int ks = i; while (i < n && s[i] != '"') i++;
                    string key = s.Substring(ks, i - ks); i++;
                    while (i < n && s[i] != ':') i++; i++;
                    while (i < n && char.IsWhiteSpace(s[i])) i++;
                    string val;
                    if (i < n && s[i] == '"')
                    {
                        i++; int vs = i; while (i < n && s[i] != '"') i++;
                        val = s.Substring(vs, i - vs); i++;
                    }
                    else
                    {
                        int vs = i; while (i < n && s[i] != ',') i++;
                        val = s.Substring(vs, i - vs).Trim();
                    }
                    j.kv[key] = val;
                    while (i < n && s[i] != ',') i++; i++;
                }
                return j;
            }
            public string Get(string k)
            {
                string v;
                return kv.TryGetValue(k, out v) ? v : "";
            }
            public double Num(string k)
            {
                string v;
                double d;
                return kv.TryGetValue(k, out v) && double.TryParse(v, NumberStyles.Any, CultureInfo.InvariantCulture, out d) ? d : 0.0;
            }
        }
    }
}
