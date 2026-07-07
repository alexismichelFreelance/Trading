// EngineRelay.cs — NinjaTrader 8 NinjaScript relay for the Python trading engine.
//
// REFERENCE IMPLEMENTATION — compile in NT8 (New > NinjaScript Editor > Strategy),
// then add EngineRelay to an ES chart running on your SIM account with
// "Calculate = On each tick". It exposes two local sockets speaking the engine's
// line-delimited JSON protocol (engine/adapters/protocol.py):
//
//   market port 36001 (engine's NinjaTraderFeed connects): trade / quote / depth
//   broker port 36002 (engine's NinjaTraderBroker connects): reads place/cancel/
//                       modify, submits to the account, streams fill/position back
//
// A Strategy is the lowest-friction host because it already receives
// OnMarketData / OnMarketDepth / OnExecutionUpdate / OnPositionUpdate. All
// NT-specific code stays here; the Python core is untouched.
//
// Notes / adjust for your NT8 version:
//  - Aggressor side is inferred from last-vs-bid/ask (NT L1 doesn't tag it).
//  - Timestamps are converted to epoch NANOSECONDS UTC.
//  - Orders are submitted unmanaged via Account.CreateOrder/Submit so arbitrary
//    engine orders (not strategy signals) route to the SIM account.
#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public class EngineRelay : Strategy
    {
        private const int MarketPort = 36001;
        private const int BrokerPort = 36002;

        private TcpListener marketListener, brokerListener;
        private readonly List<TcpClient> marketClients = new List<TcpClient>();
        private readonly List<TcpClient> brokerClients = new List<TcpClient>();
        private readonly object mLock = new object(), bLock = new object();
        private readonly Dictionary<string, Order> live = new Dictionary<string, Order>();
        // historical 1m bars buffered during State.Historical; sent to each market
        // client on connect so the engine's bar-driven features warm up instantly
        private readonly List<string> histBars = new List<string>();
        private double lastBid = 0, lastAsk = 0;
        private static readonly DateTime Epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
        // draws MUST run in a bar/market-data context, not the socket thread —
        // queue them here and flush inside OnMarketData/OnBarUpdate.
        private readonly List<MiniJson> drawQueue = new List<MiniJson>();
        private readonly object dLock = new object();
        private int rxDraws = 0, execDraws = 0, errDraws = 0;
        private string lastDrawErr = "";
        private bool canaryDrawn = false;
        private DateTime lastDrawLog = DateTime.MinValue;
        private static readonly string LogPath = System.IO.Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
            "NinjaTrader 8", "engine_relay.log");

        private void RLog(string msg)
        {
            try { System.IO.File.AppendAllText(LogPath,
                DateTime.Now.ToString("HH:mm:ss.fff") + "  " + msg + "\n"); }
            catch { }
        }

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "EngineRelay";
                Calculate = Calculate.OnEachTick;
                IsUnmanaged = true;                 // submit arbitrary orders ourselves
                EntriesPerDirection = 100;
                BarsRequiredToTrade = 0;
                // SAFETY: the relay is a pure conduit + chart painter. It must
                // NEVER auto-manage positions it did not place — otherwise a
                // MANUAL position on the same account gets flattened at session
                // close and the chart's execution display is hijacked. All exits
                // are placed explicitly by the Python engine.
                IsExitOnSessionCloseStrategy = false;   // never auto-flatten anything
                IsAdoptAccountPositionAware = false;    // account/manual pos is NOT ours
                StartBehavior = StartBehavior.ImmediatelySubmit;  // don't wait-until-flat
            }
            else if (State == State.Realtime)
            {
                // account-level events: strategy overrides do NOT fire for orders
                // submitted via Account.Submit, so subscribe to the account itself.
                Account.ExecutionUpdate += OnAccountExecution;
                Account.PositionUpdate += OnAccountPosition;
                StartServer(ref marketListener, MarketPort, marketClients, mLock, false);
                StartServer(ref brokerListener, BrokerPort, brokerClients, bLock, true);
                Print("EngineRelay: market:" + MarketPort + " broker:" + BrokerPort);
                RLog("=== EngineRelay realtime. market:" + MarketPort + " broker:" +
                     BrokerPort + " chart=" + (ChartControl != null) + " ===");
            }
            else if (State == State.Terminated)
            {
                try
                {
                    if (Account != null)
                    {
                        Account.ExecutionUpdate -= OnAccountExecution;
                        Account.PositionUpdate -= OnAccountPosition;
                    }
                    if (marketListener != null) marketListener.Stop();
                    if (brokerListener != null) brokerListener.Stop();
                }
                catch { }
            }
        }

        private static long ToNs(DateTime t)
        {
            return (long)((t.ToUniversalTime() - Epoch).Ticks) * 100L;   // 1 tick = 100 ns
        }

        private static string J(double d) { return d.ToString("R", CultureInfo.InvariantCulture); }

        // ── historical 1m bars → backfill buffer; realtime → canary + draw flush ──
        protected override void OnBarUpdate()
        {
            if (State == State.Historical && CurrentBar >= 0
                && BarsPeriod.BarsPeriodType == BarsPeriodType.Minute && BarsPeriod.Value == 1)
            {
                histBars.Add("{\"t\":\"bar\",\"ts\":" + ToNs(Time[0]) + ",\"tf\":\"1m\",\"o\":" + J(Open[0]) +
                    ",\"h\":" + J(High[0]) + ",\"l\":" + J(Low[0]) + ",\"c\":" + J(Close[0]) +
                    ",\"v\":" + ((long)Volume[0]) + "}");
                if (histBars.Count > 20000)
                    histBars.RemoveAt(0);
                return;
            }
            if (State == State.Realtime)
            {
                DrawCanary();
                FlushDraws();
            }
        }

        // proof-of-life the user cannot miss: relay draws its OWN status the first
        // realtime bar, independent of the Python painter. If this shows, the draw
        // pipe works and any missing overlays are a painter/viewport issue.
        private void DrawCanary()
        {
            if (canaryDrawn)
                return;
            canaryDrawn = true;
            try
            {
                Draw.TextFixed(this, "eng-canary", "EngineRelay: drawing OK (waiting for engine)",
                               TextPosition.BottomLeft);
                RLog("canary drawn OK (chart=" + (ChartControl != null) + ")");
            }
            catch (Exception ex) { RLog("canary FAILED: " + ex.Message); }
        }

        // execute queued draws in THIS (bar/market-data) context — the only place
        // NT8 Draw.* reliably renders.
        private void FlushDraws()
        {
            List<MiniJson> batch = null;
            lock (dLock)
            {
                if (drawQueue.Count > 0)
                {
                    batch = new List<MiniJson>(drawQueue);
                    drawQueue.Clear();
                }
            }
            if (batch != null)
                foreach (MiniJson m in batch)
                    HandleDraw(m);
            if ((DateTime.Now - lastDrawLog).TotalSeconds >= 5 && (rxDraws > 0 || execDraws > 0))
            {
                lastDrawLog = DateTime.Now;
                RLog("draws rx=" + rxDraws + " exec=" + execDraws + " err=" + errDraws +
                     (lastDrawErr.Length > 0 ? " lastErr=" + lastDrawErr : ""));
            }
        }

        // ── market data → market clients ─────────────────────────────────
        protected override void OnMarketData(MarketDataEventArgs e)
        {
            long ns = ToNs(e.Time);
            if (e.MarketDataType == MarketDataType.Bid) lastBid = e.Price;
            else if (e.MarketDataType == MarketDataType.Ask) lastAsk = e.Price;
            else if (e.MarketDataType == MarketDataType.Last)
            {
                int aggressor = e.Price >= lastAsk && lastAsk > 0 ? 1 : (e.Price <= lastBid && lastBid > 0 ? -1 : 1);
                Broadcast(marketClients, mLock,
                    "{\"t\":\"trade\",\"ts\":" + ns + ",\"price\":" + J(e.Price) +
                    ",\"size\":" + e.Volume + ",\"aggressor\":" + aggressor + "}");
            }
            if (State == State.Realtime)      // draws render reliably from here
            {
                DrawCanary();
                FlushDraws();
            }
        }

        // ── L2 depth → market clients (Python aggregates BookFlow) ────────
        protected override void OnMarketDepth(MarketDepthEventArgs e)
        {
            int side = e.MarketDataType == MarketDataType.Bid ? 1 : -1;   // +1 bid, -1 ask
            long ns = ToNs(e.Time);
            // Operation Remove -> size 0 at that price; Add/Update -> current volume.
            long size = e.Operation == Operation.Remove ? 0 : e.Volume;
            Broadcast(marketClients, mLock,
                "{\"t\":\"depth\",\"ts\":" + ns + ",\"side\":" + side + ",\"price\":" + J(e.Price) +
                ",\"size\":" + size + ",\"level\":" + e.Position + "}");
        }

        // ── ACCOUNT executions / positions → broker clients ──────────────
        // (account events fire for every instrument on the account — filter to
        // the chart's instrument so manual trades elsewhere don't leak in)
        private void OnAccountExecution(object sender, ExecutionEventArgs e)
        {
            if (e.Execution == null || e.Execution.Instrument == null
                || e.Execution.Instrument.MasterInstrument.Name != Instrument.MasterInstrument.Name)
                return;
            int qty = e.Execution.Quantity;
            int signed = e.Execution.MarketPosition == MarketPosition.Long ? qty : -qty;
            string name = e.Execution.Order != null ? e.Execution.Order.Name : "";
            Broadcast(brokerClients, bLock,
                "{\"t\":\"fill\",\"ts\":" + ToNs(e.Execution.Time) + ",\"order_id\":\"" + name +
                "\",\"symbol\":\"" + Instrument.MasterInstrument.Name +
                "\",\"price\":" + J(e.Execution.Price) + ",\"size\":" + signed +
                ",\"commission\":" + J(e.Execution.Commission) + ",\"tag\":\"" + name + "\"}");
        }

        private void OnAccountPosition(object sender, PositionEventArgs e)
        {
            if (e.Position == null || e.Position.Instrument == null
                || e.Position.Instrument.MasterInstrument.Name != Instrument.MasterInstrument.Name)
                return;
            int signed = e.Position.MarketPosition == MarketPosition.Long ? e.Position.Quantity
                       : (e.Position.MarketPosition == MarketPosition.Short ? -e.Position.Quantity : 0);
            if (e.Operation == Operation.Remove) signed = 0;
            Broadcast(brokerClients, bLock,
                "{\"t\":\"position\",\"ts\":" + ToNs(DateTime.UtcNow) + ",\"symbol\":\"" +
                Instrument.MasterInstrument.Name + "\",\"qty\":" + signed +
                ",\"avg_px\":" + J(e.Position.AveragePrice) + "}");
        }

        // ── broker socket: parse order JSON, submit to the account ────────
        private void HandleOrderLine(string line)
        {
            var m = MiniJson.Parse(line);
            string t = m.Get("t");
            if (t == "place")
            {
                OrderAction action = (int)m.Num("side") > 0 ? OrderAction.Buy : OrderAction.Sell;
                OrderType type = m.Get("otype") == "LIMIT" ? OrderType.Limit
                               : m.Get("otype") == "STOP" ? OrderType.StopMarket : OrderType.Market;
                double limit = type == OrderType.Limit ? m.Num("limit") : 0;
                double stop = type == OrderType.StopMarket ? m.Num("stop") : 0;
                Order o = Account.CreateOrder(Instrument, action, type, OrderEntry.Manual,
                    TimeInForce.Day, (int)m.Num("qty"), limit, stop, string.Empty,
                    m.Get("order_id"), Core.Globals.MaxDate, null);
                live[m.Get("order_id")] = o;
                Account.Submit(new[] { o });
            }
            else if (t == "cancel")
            {
                Order o;
                if (live.TryGetValue(m.Get("order_id"), out o)) Account.Cancel(new[] { o });
            }
            else if (t == "modify")
            {
                Order o;
                if (live.TryGetValue(m.Get("order_id"), out o))
                {
                    if (m.Num("limit") > 0) o.LimitPriceChanged = m.Num("limit");
                    if (m.Num("stop") > 0) o.StopPriceChanged = m.Num("stop");
                    if (m.Num("qty") > 0) o.QuantityChanged = (int)m.Num("qty");
                    Account.Change(new[] { o });
                }
            }
            else if (t == "draw")
            {
                lock (dLock) drawQueue.Add(m);      // flushed in OnMarketData/OnBarUpdate
                rxDraws++;
            }
        }

        // ── chart drawing from the engine ("draw" messages on the broker
        //    socket) — makes the NT8 chart the engine's visual surface: zones,
        //    ghost/live signals, trail lines, status box. Same-tag redraw
        //    REPLACES the object (NT semantics), so movable lines are cheap. ──
        private static DateTime FromNs(double ns)
        {
            return Epoch.AddTicks((long)(ns / 100.0)).ToLocalTime();
        }

        private Brush BrushOf(string spec, Brush fallback)
        {
            if (string.IsNullOrEmpty(spec))
                return fallback;
            try
            {
                Brush b = (Brush)new BrushConverter().ConvertFromString(spec);
                b.Freeze();
                return b;
            }
            catch { return fallback; }
        }

        private void HandleDraw(MiniJson m)
        {
            string kind = m.Get("kind");
            string tag = m.Get("tag");
            if (tag.Length == 0)
                tag = "eng-" + Guid.NewGuid().ToString("N").Substring(0, 8);
            try
            {
                execDraws++;
                if (kind == "arrow")
                {
                    Brush b = BrushOf(m.Get("color"), m.Num("dir") > 0 ? Brushes.LimeGreen : Brushes.Red);
                    if (m.Num("dir") > 0)
                        Draw.ArrowUp(this, tag, false, FromNs(m.Num("ts")), m.Num("price"), b);
                    else
                        Draw.ArrowDown(this, tag, false, FromNs(m.Num("ts")), m.Num("price"), b);
                    string lbl = m.Get("label");
                    if (lbl.Length > 0)
                        Draw.Text(this, tag + "-l", false, lbl, FromNs(m.Num("ts")),
                                  m.Num("price") + (m.Num("dir") > 0 ? -3.0 : 3.0), 0, b,
                                  null, System.Windows.TextAlignment.Center,
                                  Brushes.Transparent, Brushes.Transparent, 0);
                }
                else if (kind == "rect")
                {
                    Brush b = BrushOf(m.Get("color"), Brushes.SteelBlue);
                    Draw.Rectangle(this, tag, false, FromNs(m.Num("t1")), m.Num("p1"),
                                   FromNs(m.Num("t2")), m.Num("p2"), Brushes.Transparent, b,
                                   (int)(m.Num("opacity") > 0 ? m.Num("opacity") : 18));
                }
                else if (kind == "hline")
                {
                    Draw.HorizontalLine(this, tag, m.Num("price"),
                                        BrushOf(m.Get("color"), Brushes.Orange));
                }
                else if (kind == "line")
                {
                    Draw.Line(this, tag, false, FromNs(m.Num("t1")), m.Num("p1"),
                              FromNs(m.Num("t2")), m.Num("p2"),
                              BrushOf(m.Get("color"), Brushes.Orange), DashStyleHelper.Dash, 1);
                }
                else if (kind == "text")
                {
                    Draw.Text(this, tag, false, m.Get("label"), FromNs(m.Num("ts")),
                              m.Num("price"), 0, BrushOf(m.Get("color"), Brushes.Gray),
                              null, System.Windows.TextAlignment.Center,
                              Brushes.Transparent, Brushes.Transparent, 0);
                }
                else if (kind == "status")
                {
                    Draw.TextFixed(this, tag.Length > 0 ? tag : "eng-status",
                                   m.Get("label").Replace("\\n", "\n"), TextPosition.TopRight);
                }
                else if (kind == "remove")
                {
                    RemoveDrawObject(tag);
                    RemoveDrawObject(tag + "-l");
                }
            }
            catch (Exception ex)
            {
                errDraws++;
                lastDrawErr = kind + ":" + ex.Message;
                RLog("DRAW ERROR (" + kind + "/" + tag + "): " + ex.Message);
            }
        }

        // ── tiny socket plumbing ─────────────────────────────────────────
        private void StartServer(ref TcpListener listener, int port, List<TcpClient> clients, object lk, bool isBroker)
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
                    if (!isBroker) SendBackfill(c);          // warmup bars first
                    lock (lk) clients.Add(c);
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
                lock (histBars)
                {
                    for (int i = 0; i < histBars.Count; i++)
                        sb.Append(histBars[i]).Append('\n');
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
                            TriggerCustomEvent(o => HandleOrderLine(line), null);   // run on NT thread
                    }
                }
            }
            catch { }
        }

        private void Broadcast(List<TcpClient> clients, object lk, string json)
        {
            byte[] bytes = Encoding.UTF8.GetBytes(json + "\n");
            lock (lk)
            {
                for (int i = clients.Count - 1; i >= 0; i--)
                {
                    try { clients[i].GetStream().Write(bytes, 0, bytes.Length); }
                    catch { try { clients[i].Close(); } catch { } clients.RemoveAt(i); }
                }
            }
        }
    }

    // ── minimal flat-object JSON reader (NT8 ships no JSON); same file, same
    //    namespace as EngineRelay so it compiles as part of the one strategy. ──
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
            return kv.TryGetValue(k, out v) &&
                   double.TryParse(v, NumberStyles.Any, CultureInfo.InvariantCulture, out d) ? d : 0.0;
        }
    }
}
