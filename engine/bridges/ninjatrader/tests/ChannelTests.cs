// A NinjaTrader thread must never wait on a socket.
//
// Reproduces the 2026-07-30 symptom at its source: a client that connects and
// then stops reading (a busy or stalled Python side). The old Broadcast did a
// blocking NetworkStream.Write inside a lock, straight from OnMarketDepth --
// which fires 339/s on average and up to 1,218/s. Once the send buffer fills,
// that Write never returns, and NT8's market-data thread is stuck inside our
// code.
//
// Build + run (no NinjaTrader assemblies needed):
//   csc /nologo /out:ChannelTests.exe RelayChannel.cs tests\ChannelTests.cs
//   ChannelTests.exe
// csc lives at C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe
using System;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using EngineBridge;

public static class ChannelTests
{
    // A depth message as EngineRelay actually emits one (~90 bytes).
    const string DEPTH =
        "{\"t\":\"depth\",\"ts\":1785600000123456789,\"side\":1,\"price\":7439.75,\"size\":42,\"level\":3}";

    const int MESSAGES = 20000;       // ~25s of real depth at measured 800/s
    const int BUDGET_MS = 2000;       // a producer must never be stuck this long

    static int failures = 0;

    static void Check(bool ok, string name, string detail)
    {
        Console.WriteLine((ok ? "  PASS  " : "  FAIL  ") + name + "   " + detail);
        if (!ok) failures++;
    }

    /// <summary>Listener that ACCEPTS but never reads a byte — a stalled consumer.</summary>
    static TcpListener StartDeafListener(out int port)
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        port = ((IPEndPoint)l.LocalEndpoint).Port;
        var t = new Thread(() =>
        {
            try { var c = l.AcceptTcpClient(); c.ReceiveBufferSize = 1024; Thread.Sleep(Timeout.Infinite); }
            catch { }
        });
        t.IsBackground = true;
        t.Start();
        return l;
    }

    static TcpClient ConnectTo(int port)
    {
        var c = new TcpClient();
        c.Connect(IPAddress.Loopback, port);
        c.SendBufferSize = 1024;      // fill fast, so the test is quick not lucky
        return c;
    }

    static void TestLegacyBlocks()
    {
        int port; var l = StartDeafListener(out port);
        var ch = new LegacyChannel();
        ch.AddClient(ConnectTo(port));
        var sw = Stopwatch.StartNew();
        int sent = 0;
        var producer = new Thread(() =>
        {
            for (int i = 0; i < MESSAGES; i++) { ch.Publish(DEPTH); sent = i + 1; }
        });
        producer.IsBackground = true;
        producer.Start();
        producer.Join(BUDGET_MS);
        sw.Stop();
        bool stuck = producer.IsAlive;
        Check(stuck,
              "legacy Broadcast blocks the producer (THE DEFECT)",
              "sent " + sent + "/" + MESSAGES + " in " + sw.ElapsedMilliseconds +
              "ms, thread " + (stuck ? "STUCK inside Write()" : "finished"));
        l.Stop();
    }

    static void TestChannelNeverBlocks()
    {
        int port; var l = StartDeafListener(out port);
        // capacity deliberately BELOW the message count, so the overflow path is
        // exercised rather than merely available
        using (var ch = new RelayChannel("depth", MESSAGES / 4))
        {
            ch.AddClient(ConnectTo(port));
            var sw = Stopwatch.StartNew();
            for (int i = 0; i < MESSAGES; i++) ch.Publish(DEPTH);
            sw.Stop();
            Check(sw.ElapsedMilliseconds < BUDGET_MS,
                  "RelayChannel never blocks the producer",
                  MESSAGES + " publishes in " + sw.ElapsedMilliseconds + "ms " +
                  "(dropped " + ch.Dropped + ", backlog " + ch.Backlog + ")");
            Check(ch.Dropped > 0,
                  "backlog is dropped, not queued without bound",
                  "dropped " + ch.Dropped + " of " + MESSAGES);
        }
        l.Stop();
    }

    /// <summary>A stalled DEPTH consumer must not delay a FILL on another channel.
    /// The old code shared one lock per client list; a fill queued behind L2.</summary>
    static void TestSlowDepthDoesNotDelayFills()
    {
        int dport; var dl = StartDeafListener(out dport);
        int bport; var bl = StartDeafListener(out bport);
        using (var depth = new RelayChannel("depth", 5000))
        using (var broker = new RelayChannel("broker", 5000))
        {
            depth.AddClient(ConnectTo(dport));
            broker.AddClient(ConnectTo(bport));
            for (int i = 0; i < MESSAGES; i++) depth.Publish(DEPTH);   // firehose, stalled
            var sw = Stopwatch.StartNew();
            broker.Publish("{\"t\":\"fill\",\"order_id\":\"O1\",\"size\":1}");
            sw.Stop();
            Check(sw.ElapsedMilliseconds < 50,
                  "a fill is not delayed by a stalled depth consumer",
                  "fill publish took " + sw.ElapsedMilliseconds + "ms");
        }
        dl.Stop(); bl.Stop();
    }

    static void TestHealthyClientGetsEverything()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        int port = ((IPEndPoint)l.LocalEndpoint).Port;
        int received = 0;
        var reader = new Thread(() =>
        {
            var c = l.AcceptTcpClient();
            var s = c.GetStream();
            var buf = new byte[65536];
            int n;
            try { while ((n = s.Read(buf, 0, buf.Length)) > 0)
                  { for (int i = 0; i < n; i++) if (buf[i] == (byte)'\n') received++; } }
            catch { }
        });
        reader.IsBackground = true;
        reader.Start();
        const int N = 5000;
        using (var ch = new RelayChannel("ok", 20000))
        {
            ch.AddClient(ConnectTo(port));
            for (int i = 0; i < N; i++) ch.Publish(DEPTH);
            var sw = Stopwatch.StartNew();
            while (received < N && sw.ElapsedMilliseconds < 5000) Thread.Sleep(10);
            Check(received == N && ch.Dropped == 0,
                  "a healthy client still receives every message",
                  "received " + received + "/" + N + ", dropped " + ch.Dropped);
        }
        l.Stop();
    }

    public static int Main()
    {
        Console.WriteLine("RelayChannel — a NinjaTrader thread must never wait on a socket\n");
        TestLegacyBlocks();
        TestChannelNeverBlocks();
        TestSlowDepthDoesNotDelayFills();
        TestHealthyClientGetsEverything();
        Console.WriteLine("\n" + (failures == 0 ? "ALL PASS" : failures + " FAILED"));
        return failures == 0 ? 0 : 1;
    }
}
