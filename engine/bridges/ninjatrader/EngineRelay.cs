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
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
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
        private double lastBid = 0, lastAsk = 0;
        private static readonly DateTime Epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "EngineRelay";
                Calculate = Calculate.OnEachTick;
                IsUnmanaged = true;                 // submit arbitrary orders ourselves
                EntriesPerDirection = 100;
                BarsRequiredToTrade = 0;
            }
            else if (State == State.Realtime)
            {
                StartServer(ref marketListener, MarketPort, marketClients, mLock, false);
                StartServer(ref brokerListener, BrokerPort, brokerClients, bLock, true);
                Print("EngineRelay: market:" + MarketPort + " broker:" + BrokerPort);
            }
            else if (State == State.Terminated)
            {
                try { marketListener?.Stop(); brokerListener?.Stop(); } catch { }
            }
        }

        private static long ToNs(DateTime t)
        {
            return (long)((t.ToUniversalTime() - Epoch).Ticks) * 100L;   // 1 tick = 100 ns
        }

        private static string J(double d) { return d.ToString("R", CultureInfo.InvariantCulture); }

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

        // ── executions / positions → broker clients ──────────────────────
        protected override void OnExecutionUpdate(Execution exec, string executionId, double price,
            int quantity, MarketPosition marketPosition, string orderId, DateTime time)
        {
            int signed = marketPosition == MarketPosition.Long ? quantity : -quantity;
            string tag = exec.Order != null ? exec.Order.Name : "";
            Broadcast(brokerClients, bLock,
                "{\"t\":\"fill\",\"ts\":" + ToNs(time) + ",\"order_id\":\"" + orderId + "\",\"symbol\":\"" +
                Instrument.MasterInstrument.Name + "\",\"price\":" + J(price) + ",\"size\":" + signed +
                ",\"commission\":" + J(exec.Commission) + ",\"tag\":\"" + tag + "\"}");
        }

        protected override void OnPositionUpdate(Position position, double averagePrice,
            int quantity, MarketPosition marketPosition)
        {
            int signed = marketPosition == MarketPosition.Long ? quantity : (marketPosition == MarketPosition.Short ? -quantity : 0);
            Broadcast(brokerClients, bLock,
                "{\"t\":\"position\",\"ts\":" + ToNs(DateTime.UtcNow) + ",\"symbol\":\"" +
                Instrument.MasterInstrument.Name + "\",\"qty\":" + signed + ",\"avg_px\":" + J(averagePrice) + "}");
        }

        // ── broker socket: parse order JSON, submit to the account ────────
        private void HandleOrderLine(string line)
        {
            var m = MiniJson.Parse(line);
            string t = m.Get("t");
            if (t == "place")
            {
                OrderAction action = (int)m.Num("side") > 0 ? OrderAction.Buy : OrderAction.SellShort;
                OrderType type = m.Get("otype") == "LIMIT" ? OrderType.Limit
                               : m.Get("otype") == "STOP" ? OrderType.StopMarket : OrderType.Market;
                double limit = type == OrderType.Limit ? m.Num("limit") : 0;
                double stop = type == OrderType.StopMarket ? m.Num("stop") : 0;
                Order o = Account.CreateOrder(Instrument, action, type, OrderEntry.Manual,
                    TimeInForce.Day, (int)m.Num("qty"), limit, stop, string.Empty, m.Get("order_id"), null);
                live[m.Get("order_id")] = o;
                Account.Submit(new[] { o });
            }
            else if (t == "cancel")
            {
                if (live.TryGetValue(m.Get("order_id"), out var o)) Account.Cancel(new[] { o });
            }
            else if (t == "modify")
            {
                if (live.TryGetValue(m.Get("order_id"), out var o))
                    Account.Change(new[] { o });    // set o.LimitPrice/StopPrice first in a full impl
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
                    lock (lk) clients.Add(c);
                    if (isBroker) new Thread(() => ReadBroker(c)).Start();
                }
            }) { IsBackground = true }.Start();
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
}
