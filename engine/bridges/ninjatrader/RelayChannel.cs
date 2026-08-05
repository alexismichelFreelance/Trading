// RelayChannel — the socket transport for EngineRelay, with NO NinjaTrader
// dependencies so it can be compiled and tested on its own (see tests/).
//
// WHY THIS EXISTS
// EngineRelay.Broadcast() used to do a synchronous NetworkStream.Write inside a
// lock, called straight from OnMarketDepth. Measured on 2026-07-30 that handler
// fires 339 times a second on average and peaks at 1,218/s (7.93M L2 messages in
// one RTH session across ES+NQ). Three consequences, all bad:
//
//   1. Write() blocks once the TCP send buffer fills. The caller is NinjaScript's
//      market-data thread, so a slow or stopped reader on the Python side stalls
//      NT8's own data processing. The user reported the chart running minutes
//      behind and manual trading becoming unresponsive.
//   2. The lock was held ACROSS the write, so trades, bars and FILL notifications
//      queued behind the L2 firehose.
//   3. One string concat + UTF8 encode per message: ~8M allocations a session,
//      pure GC pressure inside NT8.
//
// The rule this class enforces: a thread that belongs to NinjaTrader NEVER waits
// on a socket. Publish() only enqueues, and if a consumer cannot keep up its
// backlog is DROPPED, oldest first, and counted. Losing depth ticks is
// acceptable; stalling NT8 is not.
using System;
using System.Collections.Generic;
using System.Net.Sockets;
using System.Text;
using System.Threading;

namespace EngineBridge
{
    public class RelayChannel : IDisposable
    {
        private readonly List<TcpClient> clients = new List<TcpClient>();
        private readonly object clientLock = new object();
        private readonly Queue<byte[]> queue = new Queue<byte[]>();
        private readonly object qLock = new object();
        private readonly int capacity;
        private readonly Thread writer;
        private volatile bool running = true;

        public long Dropped;          // messages discarded because a client lagged
        public long Sent;
        public string Name;

        public RelayChannel(string name, int capacity = 20000)
        {
            Name = name;
            this.capacity = capacity;
            writer = new Thread(WriterLoop);
            writer.IsBackground = true;      // never keeps NT8 alive on shutdown
            writer.Name = "RelayChannel-" + name;
            writer.Start();
        }

        public int ClientCount { get { lock (clientLock) return clients.Count; } }
        public int Backlog { get { lock (qLock) return queue.Count; } }

        public void AddClient(TcpClient c)
        {
            try { c.NoDelay = true; } catch { }
            lock (clientLock) clients.Add(c);
        }

        /// <summary>Enqueue for delivery. NEVER blocks on a socket: this is called
        /// from NinjaTrader threads. Drops the OLDEST backlog when full, because
        /// the newest market data is the data that matters.</summary>
        public void Publish(string json)
        {
            byte[] bytes = Encoding.UTF8.GetBytes(json + "\n");
            lock (qLock)
            {
                while (queue.Count >= capacity)
                {
                    queue.Dequeue();
                    Dropped++;
                }
                queue.Enqueue(bytes);
                Monitor.Pulse(qLock);
            }
        }

        private void WriterLoop()
        {
            while (running)
            {
                byte[] bytes = null;
                lock (qLock)
                {
                    while (running && queue.Count == 0) Monitor.Wait(qLock, 200);
                    if (!running) return;
                    if (queue.Count > 0) bytes = queue.Dequeue();
                }
                if (bytes == null) continue;
                // Snapshot the client list so a slow write never holds the lock
                // that AddClient needs, and so one dead client cannot block the
                // others behind a lock.
                TcpClient[] snap;
                lock (clientLock) snap = clients.ToArray();
                for (int i = 0; i < snap.Length; i++)
                {
                    try
                    {
                        NetworkStream s = snap[i].GetStream();
                        s.Write(bytes, 0, bytes.Length);   // blocking is FINE here:
                        Sent++;                            // this is our own thread
                    }
                    catch
                    {
                        try { snap[i].Close(); } catch { }
                        lock (clientLock) clients.Remove(snap[i]);
                    }
                }
            }
        }

        public void Dispose()
        {
            running = false;
            lock (qLock) Monitor.PulseAll(qLock);
            try { writer.Join(500); } catch { }
            lock (clientLock)
            {
                foreach (TcpClient c in clients) { try { c.Close(); } catch { } }
                clients.Clear();
            }
        }
    }

    /// <summary>The ORIGINAL EngineRelay.Broadcast, kept verbatim so the test can
    /// demonstrate the defect it was written to catch. Not used in production.</summary>
    public class LegacyChannel
    {
        private readonly List<TcpClient> clients = new List<TcpClient>();
        private readonly object lk = new object();

        public void AddClient(TcpClient c) { lock (lk) clients.Add(c); }

        public void Publish(string json)
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
