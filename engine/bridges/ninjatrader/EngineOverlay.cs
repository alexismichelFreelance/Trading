// EngineOverlay.cs — NinjaTrader 8 INDICATOR that draws the Python engine's
// overlays (zones / gamma levels / S-R / signals / status) on the chart.
//
// WHY AN INDICATOR (not the strategy): a STRATEGY applied to a chart suppresses
// NinjaTrader's native order/execution display. An INDICATOR does not — it draws
// freely AND your orders keep showing the normal way. So the engine is split:
//   - EngineOverlay (this indicator) lives ON the chart and only DRAWS. It opens
//     one local socket (draw port 36004) that the engine's NTChartPainter connects
//     to and sends draw messages.
//   - EngineRelay (the strategy) does data + order routing. It hides orders on
//     its own chart, so put it on a SEPARATE minimized ES chart you never watch.
//
// Add EngineOverlay to your MAIN ES chart; put EngineRelay on a second minimized
// ES chart. Both on the SIM account. Your main chart then shows native orders AND
// engine overlays together (the indicator never suppresses order display).
#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel.DataAnnotations;
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
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    public class EngineOverlay : Indicator
    {
        // one overlay per instrument chart, each with its own draw socket
        // (ES 36004 default; NQ e.g. 36014 — must match config/live.yaml)
        [NinjaScriptProperty]
        [Display(Name = "DrawPort", GroupName = "Engine", Order = 1)]
        public int DrawPort { get; set; }           // NTChartPainter connects here

        private TcpListener drawListener;
        private readonly List<TcpClient> drawClients = new List<TcpClient>();
        private readonly object cLock = new object();
        private readonly List<MiniJsonO> drawQueue = new List<MiniJsonO>();
        private readonly object dLock = new object();
        private int rxDraws = 0, execDraws = 0, errDraws = 0;
        private string lastDrawErr = "";
        private bool canaryDrawn = false;
        private DateTime lastDrawLog = DateTime.MinValue;
        private DateTime lastRefresh = DateTime.MinValue;
        private static readonly DateTime Epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
        private static readonly string LogPath = System.IO.Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
            "NinjaTrader 8", "engine_overlay.log");

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
                Name = "EngineOverlay";
                Description = "Draws the Python engine's zones/levels/signals (indicator; never hides orders).";
                IsOverlay = true;
                Calculate = Calculate.OnEachTick;
                IsSuspendedWhileInactive = false;
                DrawOnPricePanel = true;
                DrawPort = 36004;                   // per-instrument lane port
            }
            else if (State == State.Realtime)
            {
                StartDrawServer();
                RLog("=== EngineOverlay realtime. drawPort:" + DrawPort +
                     " chart=" + (ChartControl != null) + " ===");
            }
            else if (State == State.Terminated)
            {
                try { if (drawListener != null) drawListener.Stop(); } catch { }
            }
        }

        protected override void OnBarUpdate()
        {
            if (State == State.Realtime) Pump();
        }

        protected override void OnMarketData(MarketDataEventArgs e)
        {
            if (State == State.Realtime) Pump();
        }

        // flush queued draws + repaint at most ~2/sec (ForceRefresh per tick would
        // storm the render thread and freeze the chart).
        private void Pump()
        {
            DrawCanary();
            FlushDraws();
            if ((DateTime.Now - lastRefresh).TotalMilliseconds >= 500)
            {
                lastRefresh = DateTime.Now;
                try { ForceRefresh(); }
                catch (Exception ex) { lastDrawErr = "refresh:" + ex.Message; }
            }
        }

        private void DrawCanary()
        {
            if (canaryDrawn) return;
            canaryDrawn = true;
            try
            {
                Draw.TextFixed(this, "eng-canary", "EngineOverlay: drawing OK", TextPosition.BottomRight);
                RLog("canary drawn OK (chart=" + (ChartControl != null) + ")");
            }
            catch (Exception ex) { RLog("canary FAILED: " + ex.Message); }
        }

        private void FlushDraws()
        {
            List<MiniJsonO> batch = null;
            lock (dLock)
            {
                if (drawQueue.Count > 0)
                {
                    batch = new List<MiniJsonO>(drawQueue);
                    drawQueue.Clear();
                }
            }
            if (batch != null)
                foreach (MiniJsonO m in batch)
                    HandleDraw(m);
            if ((DateTime.Now - lastDrawLog).TotalSeconds >= 5 && (rxDraws > 0 || execDraws > 0))
            {
                lastDrawLog = DateTime.Now;
                RLog("draws rx=" + rxDraws + " exec=" + execDraws + " err=" + errDraws +
                     (lastDrawErr.Length > 0 ? " lastErr=" + lastDrawErr : ""));
            }
        }

        private static DateTime FromNs(double ns)
        {
            return Epoch.AddTicks((long)(ns / 100.0)).ToLocalTime();
        }

        private Brush BrushOf(string spec, Brush fallback)
        {
            if (string.IsNullOrEmpty(spec)) return fallback;
            try { Brush b = (Brush)new BrushConverter().ConvertFromString(spec); b.Freeze(); return b; }
            catch { return fallback; }
        }

        private void HandleDraw(MiniJsonO m)
        {
            string kind = m.Get("kind");
            string tag = m.Get("tag");
            if (tag.Length == 0) tag = "eng-" + Guid.NewGuid().ToString("N").Substring(0, 8);
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
                    string ps = m.Get("pos");
                    TextPosition tp = ps == "topright" ? TextPosition.TopRight
                                    : ps == "bottomright" ? TextPosition.BottomRight
                                    : ps == "bottomleft" ? TextPosition.BottomLeft
                                    : TextPosition.TopLeft;
                    Draw.TextFixed(this, tag.Length > 0 ? tag : "eng-status",
                                   m.Get("label").Replace("\\n", "\n"), tp);
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

        private void StartDrawServer()
        {
            drawListener = new TcpListener(IPAddress.Loopback, DrawPort);
            drawListener.Start();
            var l = drawListener;
            new Thread(() =>
            {
                while (true)
                {
                    TcpClient c;
                    try { c = l.AcceptTcpClient(); } catch { break; }
                    lock (cLock) drawClients.Add(c);
                    new Thread(() => ReadDraws(c)).Start();
                }
            }) { IsBackground = true }.Start();
        }

        private void ReadDraws(TcpClient c)
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
                        {
                            MiniJsonO mm = MiniJsonO.Parse(line);
                            lock (dLock) drawQueue.Add(mm);
                            rxDraws++;
                        }
                    }
                }
            }
            catch { }
        }
    }

    // minimal flat-object JSON reader (own copy in the Indicators namespace)
    public class MiniJsonO
    {
        private readonly Dictionary<string, string> kv = new Dictionary<string, string>();

        public static MiniJsonO Parse(string s)
        {
            var j = new MiniJsonO();
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

        public string Get(string k) { string v; return kv.TryGetValue(k, out v) ? v : ""; }

        public double Num(string k)
        {
            string v; double d;
            return kv.TryGetValue(k, out v) &&
                   double.TryParse(v, NumberStyles.Any, CultureInfo.InvariantCulture, out d) ? d : 0.0;
        }
    }
}
