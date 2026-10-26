/* es-engine dashboard v2 — chart-first, engine-driven rendering.
 *
 * Renders ONLY what the engine publishes (never re-derives an indicator in
 * JS): bars/volume from the engine's BarDict, the anchored-VWAP curve + its
 * +/-2sigma / +/-3sigma bands (publisher sends value+band point series, JS
 * just pairs hi/lo), the multi-walled dealer-gamma map (ranked put/call
 * walls, flips, pocket, regime-at-price from local_sign), the strategy
 * ROSTER with show/hide toggles that add/remove that strategy's overlay
 * marks, zones, and open positions.
 *
 * Multi-window model: one ChartWindow per grid cell; each cell is one
 * symbol+timeframe with its OWN WebSocket (the server scopes each socket to
 * a window's symbol/tf). Every browser tab hosts its own cell(s); the
 * "+ add" / pop-out buttons open more tabs, and "▦ grid" toggles a 2-up
 * view. All layout is chart-first with on-demand drawers.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const qs = new URLSearchParams(location.search);

let LANES = ["ES"];
let TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];

const windows = new Map();      // id -> ChartWindow
let windowCounter = 0;

/* ── tiny helpers ─────────────────────────────────────────────────────── */
const sec = (ns) => Math.floor(ns / 1e9);
const fmtPx = (x) => x == null ? "—" : Number(x).toFixed(2);
const pad = (n) => String(n).padStart(2, "0");
/* LWC throws "Value is null" on a null/NaN in setData/update — and one
 * throwing series setData corrupts the shared chart model (blank chart).
 * Indicator points from the engine can legitimately carry nulls (sigma
 * bands undefined early-session), so nothing invalid ever reaches LWC. */
const validPt = (p) => p && typeof p.ts === 'number' &&
  typeof p.v === 'number' && !isNaN(p.v);
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ══════════════════ ChartWindow — one symbol/tf cell ══════════════════ */
class ChartWindow {
  constructor(id, symbol, tf, container) {
    this.id = id;
    this.symbol = symbol;
    this.tf = tf;
    this.ws = null;

    this.candles = [];
    this.indicators = {};       // id -> {id, producer, label, points:[]}
    this.gamma = null;
    this.roster = [];           // [{label, enabled, qty, entry, target, stop}]
    this.strategyOn = {};       // label -> bool
    this.strategySolo = null;
    this.indicatorOn = {};      // id -> bool
    this.zones = [];            // [{id, tf, top, bot, direction, virgin, touches}]
    this.positions = [];        // [{sleeve, qty, entry, target, stop}]
    this.fillMarkers = [];
    this.signalMarkers = [];
    this._logCount = 0;
    this._reconnectDelay = 1500;
    this._initialLoadDone = false;
    this._autoScroll = true;
    this._loadingHistory = false;
    this._noMoreHistory = false;

    // series / line registries
    this.indSeries = {};        // id -> line series (vwap family line series)
    this.staticLines = [];      // pivot / gamma-level horizontal lines
    this.zoneLines = [];        // zone edge horizontal lines
    this.gammaLines = [];
    this.strategyLines = {};    // label -> [priceLine, ...]
    this.positionLines = [];
    this.bandSeries = {};       // id -> line series for vwap hi/lo

    this.buildDom(container);
    this.buildCharts();
    this.buildSeries();
    this.reflow();
    // redraw zone rectangles on pan/zoom
    try {
      this.chart.timeScale().subscribeVisibleTimeRangeChange(() => {
        this.drawZoneRects();
        this.maybeLoadOlder();
      });
    } catch (e) {}
  }

  buildDom(container) {
    this.wrapper = document.createElement("section");
    this.wrapper.className = "chartWindowW";
    this.wrapper.innerHTML = `
      <div class="chartWindow">
        <div class="windowHead">
          <span class="winTitle"></span>
          <span class="winSpacer"></span>
          <button class="iconBtn markBtn" data-action="strategy" title="this window's strategies">⚙</button>
          <button class="iconBtn markBtn" data-action="gamma" title="dealer-gamma map">γ</button>
          <button class="iconBtn popBtn" title="open this window in its own tab">⤢</button>
          <button class="iconBtn closeBtn hidden" title="close this window">✕</button>
        </div>
        <div class="chartCol">
          <div class="chartBox"></div>
          <div class="volBox"></div>
        </div>
      </div>`;
    this.chartBox = this.wrapper.querySelector(".chartBox");
    this.volBox = this.wrapper.querySelector(".volBox");
    // canvas overlay for zone rectangles
    this.zoneCanvas = document.createElement("canvas");
    this.zoneCanvas.style.cssText = "position:absolute;inset:0;pointer-events:none;z-index:2;";
    this.chartBox.appendChild(this.zoneCanvas);
    // session shading canvas (behind zones)
    this.sessionCanvas = document.createElement("canvas");
    this.sessionCanvas.style.cssText = "position:absolute;inset:0;pointer-events:none;z-index:1;";
    this.chartBox.appendChild(this.sessionCanvas);
    this.sessionCanvas.style.zIndex = "1";
    // floating right-side label panel (pinned labels, not overlaying price)
    this.levelLabels = document.createElement("div");
    this.levelLabels.className = "levelLabels";
    this.chartBox.appendChild(this.levelLabels);
    this.winTitle = this.wrapper.querySelector(".winTitle");
    this.closeBtn = this.wrapper.querySelector(".closeBtn");
    this.popBtn = this.wrapper.querySelector(".popBtn");
    this.stratBtn = this.wrapper.querySelector('[data-action="strategy"]');
    this.gammaBtn = this.wrapper.querySelector('[data-action="gamma"]');

    this.closeBtn.addEventListener("click", () => this.close());
    this.popBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      openWindowTab(this.symbol, this.tf);
    });
    container.appendChild(this.wrapper);
  }

  buildCharts() {
    this.chart = LightweightCharts.createChart(this.chartBox, {
      layout: { background: { type: "solid", color: "#0b0e14" },
                textColor: "#dbe2ea", fontSize: 11 },
      grid: { vertLines: { color: "#161c26" }, horzLines: { color: "#161c26" } },
      crosshair: { mode: 0 },
      timeScale: { visible: true, timeVisible: true, secondsVisible: false,
                   borderColor: "#232c3b", rightOffset: 3 },
      rightPriceScale: { borderColor: "#232c3b" },
      autoSize: false,
      width: 0, height: 0,
    });
    const lwcContainer = this.chartBox.querySelector("div");
    if (lwcContainer) lwcContainer.style.cssText += ";position:relative;z-index:1;";
    // re-render floating labels on zoom/scroll/resize
    this.chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
      this.renderLevelLabels();
    });
    this.chart.timeScale().subscribeVisibleTimeRangeChange(() => {
      this.renderLevelLabels();
    });

  }

  buildSeries() {
    /* VWAP sigma bands as native LWC area series (no canvas overlay).
     *
     * An area series fills from its line DOWN to the bottom of the pane with
     * a topColor->bottomColor gradient (topColor sits on the line). So each
     * band is TWO series: a fill on the band's UPPER edge (gradient wash
     * fading downward) and a MASK on the band's LOWER edge filled with the
     * opaque chart background, which erases the fill below the band.
     *
     * Creation order = z-order (bottom -> top), and it is load-bearing:
     * every fill must sit above the masks of the bands ABOVE it (so those
     * masks can clip it) and below nothing else. The verified stack:
     *   1. mask@2s_hi   (opaque)   5. fill@vwap    (strong, lower inner)
     *   2. fill@3s_hi   (light)    6. mask@2s_lo   (opaque)
     *   3. mask@vwap    (opaque)   7. fill@2s_lo   (light, lower outer)
     *   4. fill@2s_hi   (strong)   8. mask@3s_lo   (opaque)
     * which yields exactly: strong fill VWAP<->2s, light fill 2s<->3s, and
     * clean background everywhere else — on BOTH sides.
     *
     * All band series are created BEFORE the candle series, so the opaque
     * masks sit behind the candles while the dashed edge lines and VWAP line
     * (created after) stay on top. Because these are real series on the main
     * price scale, the bands track the crosshair, price scale and visible
     * range exactly like TradingView's native bands. */
    const BG = "#0b0e14";
    const bandFill = (top, bottom) => ({
      topColor: top, bottomColor: bottom,
      lineVisible: false, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerVisible: false,
    });
    const bandMask = () => ({
      topColor: BG, bottomColor: BG,
      lineVisible: false, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerVisible: false,
    });
    // [area series, source indicator id it mirrors]
    this.bandAreas = [];
    const mkArea = (opts, src) => {
      const ser = this.chart.addAreaSeries(opts);
      this.bandAreas.push({ ser, src });
      return ser;
    };
    const STRONG = ["rgba(240,182,74,0.16)", "rgba(240,182,74,0.03)"];
    const LIGHT = ["rgba(224,139,46,0.08)", "rgba(224,139,46,0.015)"];
    mkArea(bandMask(), "vwap_2s_hi");                        // 1
    mkArea(bandFill(...LIGHT), "vwap_3s_hi");                // 2
    mkArea(bandMask(), "vwap");                              // 3
    mkArea(bandFill(...STRONG), "vwap_2s_hi");               // 4
    mkArea(bandFill(...STRONG), "vwap");                     // 5
    mkArea(bandMask(), "vwap_2s_lo");                        // 6
    mkArea(bandFill(...LIGHT), "vwap_2s_lo");                // 7
    mkArea(bandMask(), "vwap_3s_lo");                        // 8

    this.candleSeries = this.chart.addCandlestickSeries({
      upColor: "#089981", downColor: "#f23645",
      borderUpColor: "#089981", borderDownColor: "#f23645",
      wickUpColor: "#089981", wickDownColor: "#f23645",
      priceLineVisible: false,
    });

    // VWAP curve + band edge line series (all on the main chart's scale)
    this.vwapSeries = this.chart.addLineSeries({
      color: "#e2a03f", lineWidth: 2, priceLineVisible: false,
      lastValueVisible: true, crosshairMarkerVisible: false,
      lineStyle: 0,
    });
    const bandSpec = {
      vwap_2s_hi: { color: "#f0b64a", lineWidth: 1, lineStyle: 1 },
      vwap_2s_lo: { color: "#f0b64a", lineWidth: 1, lineStyle: 1 },
      vwap_3s_hi: { color: "#e08b2e", lineWidth: 1, lineStyle: 2 },
      vwap_3s_lo: { color: "#e08b2e", lineWidth: 1, lineStyle: 2 },
    };
    for (const key of ["vwap_2s_hi", "vwap_2s_lo", "vwap_3s_hi", "vwap_3s_lo"]) {
      this.bandSeries[key] = this.chart.addLineSeries({
        color: bandSpec[key].color, lineWidth: bandSpec[key].lineWidth,
        lineStyle: bandSpec[key].lineStyle || 0,
        priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
    }

    this.volSeries = this.chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
      scaleMargins: { top: 0.8, bottom: 0 },
    });
    this.chart.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });
  }

  /* Push one indicator's points into every band area that mirrors it.
   * append=true carries only the new tail (series.update per point); a
   * snapshot carries the full history (series.setData). Values come from the
   * engine-published indicator store — never re-derived here. */
  syncBandAreas(id, append, newPts) {
    const src = this.indicators[id];
    const pts = (append ? newPts : (src ? src.points : null)) || [];
    const ok = pts.filter(validPt);
    if (!ok.length) return;
    const mapped = ok.map(p => ({ time: sec(p.ts), value: p.v }));
    for (const a of this.bandAreas) {
      if (a.src !== id) continue;
      if (append) for (const p of mapped) a.ser.update(p);
      else a.ser.setData(mapped);
    }
  }

  reflow() {
    const r = (el) => el.getBoundingClientRect();
    const cw = Math.max(200, Math.floor(r(this.chartBox).width));
    const ch = Math.max(120, Math.floor(r(this.chartBox).height));
    this.chart.resize(cw, ch);
    this.drawZoneRects();
    this.drawSessionShading();
  }

  /* ── data ──────────────────────────────────────────────────────────── */
  setCandles(list) {
    this.candles = (list || []).filter(b =>
      b && typeof b.ts === 'number' &&
      typeof b.o === 'number' && !isNaN(b.o) &&
      typeof b.h === 'number' && !isNaN(b.h) &&
      typeof b.l === 'number' && !isNaN(b.l) &&
      typeof b.c === 'number' && !isNaN(b.c)
    );
    // LightweightCharts needs strictly ascending, unique time: a backfill
    // can deliver older bars after newer ones, so defensive-sort + dedupe.
    this.candles.sort((a, b) => a.ts - b.ts);
    const seen = new Set(); const uniq = [];
    for (const c of this.candles) {
      if (seen.has(c.ts)) continue;   // keep first occurrence of a ts
      seen.add(c.ts); uniq.push(c);
    }
    this.candles = uniq;
    const cd = this.candles.map(b => ({
      time: sec(b.ts),
      open: b.o,
      high: b.h,
      low: b.l,
      close: b.c
    }));
    this.candleSeries.setData(cd);

    const vd = this.candles.map(b => ({
      time: sec(b.ts),
      value: (typeof b.v === 'number' && !isNaN(b.v)) ? b.v : 0,
      color: b.o <= b.c ? "rgba(38,166,154,0.55)" : "rgba(239,83,80,0.55)"
    }));
    this.volSeries.setData(vd);
    this.computeSessionMarkers();
    this.applyMarkers();
  }

  updateBar(b) {
    if (!b || typeof b.ts !== 'number' || typeof b.o !== 'number' ||
        typeof b.h !== 'number' || typeof b.l !== 'number' || typeof b.c !== 'number') return;
    const last = this.candles.length ? this.candles[this.candles.length - 1] : null;
    // LightweightCharts only accepts ASCENDING updates: pushing an older bar
    // onto a newer series throws "Cannot update oldest data" and blanks the
    // chart. The NT8 feed replays its startup backfill newest-first, so a
    // client that connects mid-warmup really does receive older bars after
    // newer ones -- drop them (the snapshot already carries the history).
    if (last && b.ts < last.ts) return;
    const datum = { time: sec(b.ts), open: b.o, high: b.h, low: b.l, close: b.c };
    const vdat = { time: sec(b.ts), value: (typeof b.v === 'number' && !isNaN(b.v)) ? b.v : 0,
      color: b.o <= b.c ? "rgba(38,166,154,0.55)" : "rgba(239,83,80,0.55)" };
    if (last && last.ts === b.ts) {
      this.candles[this.candles.length - 1] = b;
      this.candleSeries.update(datum);
      this.volSeries.update(vdat);
    } else {
      this.candles.push(b);
      this.candleSeries.update(datum);
      this.volSeries.update(vdat);
      this.followEnd();
    }
  }

  /* Append-aware indicator upsert.
   *
   *   append=false (snapshot): the series carries its FULL history — replace
   *   this window's copy and (re)build the line with setData().
   *
   *   append=true  (delta):   the engine's indicator deltas carry the series'
   *   accumulated history (bounded to the most recent 600 points
   *   server-side), not just the new points. We merge them onto the
   *   accumulated copy, then advance the chart series with update() for ONLY
   *   the points newer than what the series already holds — LWC rejects
   *   update() with a time <= the series' last time ("Cannot update oldest
   *   data"), so re-sending the overlapping head of the history would throw
   *   on every delta. The merge above makes the stored copy idempotent; the
   *   tail filter makes the series advance exactly one repaint per new point.
   *   This is the dashboard-side half of the append-only data flow. */
  upsertIndicator(ind, append = false) {
    if (!ind || !ind.id) return;
    const newPts = (ind.points || []).slice();
    let cur = this.indicators[ind.id];
    if (!cur) {
      cur = this.indicators[ind.id] =
        { id: ind.id, producer: "", label: "", unit: "", points: [] };
    }
    let tail = newPts;
    if (append) {
      const prevLast = cur.points.length ? cur.points[cur.points.length - 1].ts : null;
      const idx = new Map(cur.points.map((p, i) => [p.ts, i]));
      for (const p of newPts) {
        const i = idx.get(p.ts);
        if (i >= 0) cur.points[i] = p;
        else { cur.points.push(p); idx.set(p.ts, cur.points.length - 1); }
      }
      cur.producer = ind.producer || cur.producer;
      cur.label = ind.label || cur.label;
      cur.unit = ind.unit || cur.unit;
      tail = prevLast == null ? newPts.slice()
                              : newPts.filter(p => p.ts > prevLast);
      // align the advancing tail onto the candle grid too (see snapToGrid)
      tail = this.snapToGrid(tail);
    } else {
      // snapshot: the payload is the whole series — replace outright
      cur.points = newPts.slice();
      cur.producer = ind.producer || "";
      cur.label = ind.label || "";
      cur.unit = ind.unit || "";
    }
    cur.points.sort((a, b) => a.ts - b.ts);
    // A STATIC LEVEL series (floor pivots, dealer gamma) is not a time
    // series: the engine stamps the WHOLE grid for EVERY session it spans,
    // at that session's ts. Keeping one point per ts collapses the nine
    // pivot levels into one; keeping them all would draw 21 sessions of
    // grids at once. Take the CURRENT session's grid and let
    // drawStaticLines() dedupe it by price.
    const staticLevel = (cur.producer || "").includes("pivot") ||
                        (cur.producer || "").includes("gamma_levels");
    if (staticLevel) {
      const maxTs = cur.points.length ? cur.points[cur.points.length - 1].ts : null;
      cur.points = cur.points.filter((p) => p.ts === maxTs);
    } else {
      // LWC's renderer throws "Value is null" on duplicate times inside one
      // series (engine stamps several indicators at the session ts) — keep the
      // last point per ts so every series is strictly ascending.
      cur.points = cur.points.filter((p, i) =>
        i === cur.points.length - 1 || cur.points[i + 1].ts !== p.ts);
      // LWC derives the time scale's time-per-slot from the SMALLEST gap
      // between consecutive time points across ALL series. The engine emits
      // indicator points per tick (60s) while candles are per bar (300s) —
      // that dense tail collapses the whole time scale to 60s/slot and every
      // bar is painted 5x too narrow. Snap each indicator point to the
      // nearest candle time so every series shares the bar grid.
      cur.points = this.snapToGrid(cur.points);
    }

    this.renderIndicatorsList();
    if (this.indicatorOn[cur.id] === false) {
      // remove the rendered output for this indicator (data stays stored)
      if (cur.id === "vwap" || this.bandSeries[cur.id]) {
        const ser = cur.id === "vwap" ? this.vwapSeries : this.bandSeries[cur.id];
        if (ser) try { ser.setData([]); } catch {}
        if (cur.id === "vwap") for (const a of this.bandAreas) try { a.ser.setData([]); } catch {}
      } else if ((cur.producer || "").includes("pivot")) {
        const isMulti = (cur.id || "").includes("mpivots");
        const arr = isMulti ? this.pivotLinesMulti : this.pivotLinesDaily;
        for (const ser of (arr || [])) try { this.chart.removeSeries(ser); } catch {}
        if (isMulti) { this.pivotLinesMulti = []; this.pivotLabelsMulti = []; }
        else { this.pivotLinesDaily = []; this.pivotLabelsDaily = []; }
        this.renderLevelLabels();
      } else if ((cur.producer || "").includes("gamma_levels")) {
        for (const pl of (this.gammaLines || [])) try { this.candleSeries.removePriceLine(pl); } catch {}
        this.gammaLines = [];
      } else {
        const ser = this.indSeries[cur.id];
        if (ser) try { this.chart.removeSeries(ser); delete this.indSeries[cur.id]; } catch {}
      }
      return;
    }
    if (cur.id === "vwap" || this.bandSeries[cur.id]) {
      const ser = cur.id === "vwap" ? this.vwapSeries : this.bandSeries[cur.id];
      if (!ser) return;
      if (append) {
        for (const p of tail) if (validPt(p))
          ser.update({ time: sec(p.ts), value: p.v });
      } else {
        const pts = cur.points.filter(validPt);
        ser.setData(pts.map(p => ({ time: sec(p.ts), value: p.v })));
      }
      // band areas are native series now — no canvas overlay to repaint
      this.syncBandAreas(cur.id, append, tail);
      return;
    }
    // pivots are static price LEVELS (the engine stamps all their points with
    // the session ts). Render them as labelled price lines so the level name
    // (D-PP, W-R1, ...) shows on the axis. Gamma walls are handled by
    // renderGamma() (multi-wall, labelled) and must not be re-drawn here.
    if ((cur.producer || "").includes("pivot")) {
      this.drawPivotLines(cur);
      return;
    }
    if ((cur.producer || "").includes("gamma_levels")) {
      return;
    }
    // unknown non-pivot, non-vwap series: draw as a generic line
    let ser = this.indSeries[cur.id];
    if (!ser) {
      ser = this.chart.addLineSeries({ color: "#a78bfa", lineWidth: 1,
        priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false });
      this.indSeries[cur.id] = ser;
    }
    if (append) {
      for (const p of tail) if (validPt(p))
        ser.update({ time: sec(p.ts), value: p.v });
    } else {
      const pts = cur.points.filter(validPt);
      ser.setData(pts.map(p => ({ time: sec(p.ts), value: p.v })));
    }
  }

  /* Snap indicator points onto the candle time grid (single forward scan —
   * both inputs are sorted ascending). Points farther than `tol` from any
   * candle are dropped; duplicates are left for the caller's dedupe. */
  snapToGrid(pts, tol = 200) {
    const g = this.candles;
    if (!g.length || !pts.length) return pts;
    const out = [];
    let gi = 0, lastTs = null;
    for (const p of pts) {
      const t = sec(p.ts);
      while (gi < g.length - 1 &&
             Math.abs(sec(g[gi + 1].ts) - t) <= Math.abs(sec(g[gi].ts) - t)) gi++;
      const snapped = sec(g[gi].ts);
      if (Math.abs(snapped - t) > tol) continue;
      if (snapped === lastTs) out[out.length - 1].v = p.v;  // keep latest
      else { out.push({ ts: g[gi].ts, v: p.v }); lastTs = snapped; }
    }
    return out;
  }

  nowTs() {
    const last = this.candles.length ? this.candles[this.candles.length - 1] : null;
    return last ? sec(last.ts) : Math.floor(Date.now() / 1000);
  }

  /* Bar spacing (seconds) of the candle grid — the width of one slot on the
   * time axis. */
  gridStep() {
    const c = this.candles;
    if (c.length < 2) return 300;
    const n = Math.min(c.length - 1, 20);
    let sum = 0;
    for (let i = c.length - n; i < c.length; i++) sum += (c[i].ts - c[i - 1].ts) / 1e9;
    const step = sum / n;
    return (step > 0 && isFinite(step)) ? step : 300;
  }

  /* Nearest candle time (epoch SECONDS) for an arbitrary instant, clamped to
   * the grid's bounds.
   *
   * EVERY series on the chart has to share one time grid. LWC derives the
   * time scale's slot width from the SMALLEST gap between consecutive points
   * across ALL series, so one stray timestamp that sits off the bar grid
   * (a zone formed weeks earlier, a level anchored to "now - 1 day") either
   * collapses the slot width to a fraction of a bar — every bar renders
   * microns wide — or stretches the scale so the bars vanish off-screen. */
  snapTimeToGrid(tSec) {
    const c = this.candles;
    if (!c.length) return tSec;
    const g0 = sec(c[0].ts), g1 = sec(c[c.length - 1].ts);
    if (tSec <= g0) return g0;
    if (tSec >= g1) return tSec;   // allow extending beyond last bar (right edge)
    let lo = 0, hi = c.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (sec(c[mid].ts) < tSec) lo = mid + 1; else hi = mid;
    }
    const up = sec(c[lo].ts);
    const dn = lo > 0 ? sec(c[lo - 1].ts) : up;
    return (tSec - dn) <= (up - tSec) ? dn : up;
  }

  /* Overlay levels share the price scale with the candles, so ONE corrupt
   * level destroys the whole chart: a zone edge at 31,159 (NQ prices the
   * recorder wrote under the ES symbol) stretches the scale 4x and squeezes
   * the candles into an invisible sliver at the bottom of the pane.
   *
   * A level more than 50% away from the current price is data corruption,
   * not market structure -- ES at 7,750 has no legitimate level at 30,700.
   * Returns null for such a level so the caller skips it. */
  priceSanity(price) {
    if (typeof price !== "number" || !isFinite(price)) return null;
    const c = this.candles;
    if (!c.length) return price;
    const ref = c[c.length - 1].c;
    if (!ref || !isFinite(ref) || ref <= 0) return price;
    if (price > ref * 1.5 || price < ref * 0.5) return null;
    return price;
  }

  /* A horizontal LEVEL line (gamma wall, pivot, zone edge, position level).
   * Times are epoch SECONDS. Both endpoints are snapped onto the candle grid
   * and forced strictly ascending: LWC throws "Value is null" when a series
   * carries the same timestamp twice, and that error blanks the whole chart. */
  drawBoundedLevel(price, startSec, endSec, color, lineStyle, lineWidth) {
    if (price == null) return null;
    price = this.priceSanity(price);
    if (price == null) return null;
    let t0 = startSec, t1 = endSec;
    if (this.candles.length) {
      const c = this.candles;
      const g0 = sec(c[0].ts), g1 = sec(c[c.length - 1].ts);
      const step = this.gridStep();
      t0 = t0 == null ? g0 : this.snapTimeToGrid(t0);
      t1 = t1 == null ? g1 : this.snapTimeToGrid(t1);
      if (t1 < t0) { const s = t0; t0 = t1; t1 = s; }
      if (t1 === t0) { t0 = g0; t1 = g1; }        // degenerate: span the window
      if (t1 === t0) t1 = t0 + step;              // single-bar grid
    } else {
      const now = this.nowTs();
      if (t0 == null) t0 = now - 86400;
      if (t1 == null) t1 = now;
      if (t1 < t0) { const s = t0; t0 = t1; t1 = s; }
      if (t1 === t0) t1 = t0 + 60;
    }
    const series = this.chart.addLineSeries({
      color, lineWidth: lineWidth || 1, lineStyle: lineStyle || 0,
      priceLineVisible: false, lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    series.setData([
      { time: t0, value: price },
      { time: t1, value: price },
    ]);
    return series;
  }

  drawStaticLines(ind, color) {
    for (const ser of this.staticLines) { try { this.chart.removeSeries(ser); } catch {} }
    this.staticLines = [];
    const seen = new Set();
    for (const p of (ind && ind.points) || []) {
      const key = p.v.toFixed(4);
      if (seen.has(key)) continue;
      seen.add(key);
      // a static LEVEL spans the whole visible window (null/null -> grid ends)
      const ser = this.drawBoundedLevel(p.v, null, null,
                                        color || "#a78bfa", 2, 1);
      if (ser) this.staticLines.push(ser);
    }
  }

  /* Render floating labels on the right side for all bounded levels (gamma,
   * pivots). Labels are positioned by priceToCoordinate so they track the
   * price axis without overlaying the candle series. */
  renderLevelLabels() {
    if (!this.levelLabels) return;
    const ps = this.candleSeries;
    if (!ps) return;
    const labels = []
      .concat(this.gammaLabels || [])
      .concat(this.pivotLabelsMulti || [])
      .concat(this.pivotLabelsDaily || [])
      .concat(this.strategyLabels || [])
      .concat(this.positionLabels || []);
    // dedupe by text
    const seen = new Set();
    const uniq = [];
    for (const l of labels) {
      if (seen.has(l.text)) continue;
      seen.add(l.text);
      uniq.push(l);
    }
    this.levelLabels.innerHTML = uniq.map(l => {
      const y = ps.priceToCoordinate(l.price);
      if (y == null) return "";
      return `<div class="levelLabel" style="position:absolute;right:2px;top:${Math.max(0, y - 8)}px;color:${l.color};font-size:10px;white-space:nowrap;">${escapeHtml(l.text)}</div>`;
    }).join("");
  }

  /* Labelled pivot price lines (D/W/M). Each distinct level gets a price line
   * on the axis so its name (D-PP, W-R1, ...) is visible. Re-rendered on
   * every pivot delta. */
  drawPivotLines(ind) {
    const isMulti = (ind && (ind.producer || "")).includes("mpivots");
    if (isMulti) {
      if (!this.pivotLinesMulti) this.pivotLinesMulti = [];
      for (const ser of this.pivotLinesMulti) { try { this.chart.removeSeries(ser); } catch {} }
      this.pivotLinesMulti = [];
      this.pivotLabelsMulti = [];
    } else {
      if (!this.pivotLinesDaily) this.pivotLinesDaily = [];
      for (const ser of this.pivotLinesDaily) { try { this.chart.removeSeries(ser); } catch {} }
      this.pivotLinesDaily = [];
      this.pivotLabelsDaily = [];
    }
    const rng = this.lastSessionRange();
    const rightEdge = this.rightEdgeSec();
    const seen = new Set();
    for (const p of (ind && ind.points) || []) {
      const lbl = p.label || "";
      const key = lbl + "|" + Number(p.v).toFixed(4);
      if (seen.has(key)) continue;
      seen.add(key);
      const price = this.priceSanity(p.v);
      if (price == null) continue;
      let color = "#a78bfa";
      if (/\bPP\b/i.test(lbl)) color = "#eab308";
      else if (/\bS\d/i.test(lbl)) color = "#22c55e";
      else if (/\bR\d/i.test(lbl)) color = "#ef4444";
      const startSec = rng ? rng.from : null;
      const endSec = rightEdge;
      const ser = this.drawBoundedLevel(price, startSec, endSec, color, 2, 1);
      const arr = isMulti ? this.pivotLinesMulti : this.pivotLinesDaily;
      if (ser) arr.push(ser);
      const labArr = isMulti ? (this.pivotLabelsMulti = this.pivotLabelsMulti || []) : (this.pivotLabelsDaily = this.pivotLabelsDaily || []);
      labArr.push({ price, color, text: lbl ? `${lbl} ${price.toFixed(2)}` : `pivot ${price.toFixed(2)}` });
    }
    this.renderLevelLabels();
  }


  rightEdgeSec() {
    const c = this.candles;
    if (!c.length) return null;
    const step = this.gridStep();
    // extend well beyond the last bar so lines reach the right edge of the chart
    const ts = this.chart.timeScale();
    let vr = null;
    try { vr = ts.getVisibleRange(); } catch {}
    if (vr && vr.to) {
      return Math.round(vr.to + step * 5);
    }
    return sec(c[c.length - 1].ts) + Math.round(step * 20);
  }

  /* ── dealer-gamma: multi-wall price lines + regime in header ───────── */
  lastSessionRange() {
    const c = this.candles;
    if (!c.length) return null;
    const sessions = [];
    let cur = [c[0]];
    for (let i = 1; i < c.length; i++) {
      if ((c[i].ts - c[i-1].ts) / 1e9 > 1800) {
        sessions.push(cur); cur = [c[i]];
      } else cur.push(c[i]);
    }
    sessions.push(cur);
    const last = sessions[sessions.length - 1];
    return { from: sec(last[0].ts), to: sec(last[last.length - 1].ts) };
  }

  renderGamma(g) {
    this.gamma = g;
    if (!this.gammaLines) this.gammaLines = [];
    for (const ser of this.gammaLines) { try { this.chart.removeSeries(ser); } catch {} }
    this.gammaLines = [];
    this.gammaLabels = [];
    if (this.indicatorOn["__gamma__"] === false) {
      renderGammaDrawerFor(this);
      return;
    }
    if (!g || !g.ok) { this.setRegime(g && g.local_sign); renderGammaDrawerFor(this); return; }
    const rng = this.lastSessionRange();
    const startSec = rng ? rng.from : null;
    const endSec = this.rightEdgeSec();
    const flips = g.flips || [];
    for (const f of flips) {
      const px = this.priceSanity(f);
      if (px == null) continue;
      const ser = this.drawBoundedLevel(px, startSec, endSec, "#e6a9ff", 0, 1);
      if (ser) this.gammaLines.push(ser);
      this.gammaLabels.push({ price: px, color: "#e6a9ff", text: `flip ${px.toFixed(2)}` });
    }
    for (const w of (g.call_walls || [])) {
      const k = Array.isArray(w) ? w[0] : w;
      if (k == null) continue;
      const px = this.priceSanity(k);
      if (px == null) continue;
      const ser = this.drawBoundedLevel(px, startSec, endSec, "#ffa500", 2, 1);
      if (ser) this.gammaLines.push(ser);
      this.gammaLabels.push({ price: px, color: "#ffa500", text: `call wall ${px.toFixed(2)}` });
    }
    for (const w of (g.put_walls || [])) {
      const k = Array.isArray(w) ? w[0] : w;
      if (k == null) continue;
      const px = this.priceSanity(k);
      if (px == null) continue;
      const ser = this.drawBoundedLevel(px, startSec, endSec, "#1e90ff", 2, 1);
      if (ser) this.gammaLines.push(ser);
      this.gammaLabels.push({ price: px, color: "#1e90ff", text: `put wall ${px.toFixed(2)}` });
    }
    this.renderLevelLabels();
    this.setRegime(g && g.local_sign);
    renderGammaDrawerFor(this);
  }

  setRegime(sign) {
    let text = `${this.symbol} · ${this.tf} · live`;
    if (sign > 0) text += `<span class="regime long">LONG γ</span>`;
    else if (sign < 0) text += `<span class="regime short">SHORT γ</span>`;
    this.winTitle.innerHTML = text;
  }

  /* ── strategy roster: show/hide toggles per engine strategy ──────── */
  setRoster(roster) {
    this.roster = roster || [];
    this.strategyOn = {};
    for (const s of this.roster) this.strategyOn[s.label] = !!s.enabled;
    this.renderRoster();
    this.applyStrategyMarks();
  }

  renderRoster() {
    const host = $("roster");
    if (!host) return;
    host.innerHTML = this.roster.map((s, i) => {
      const on = this.strategyOn[s.label];
      const solo = this.strategySolo === s.label;
      return `<div class="rosterRow ${on ? "" : "off"}">
        <span class="sw ${on ? "on" : ""}" data-i="${i}"></span>
        <span class="name">${escapeHtml(s.label)}</span>
        <span class="modeTag ${s.mode === 'live' ? 'live' : 'paper'}">${s.mode === 'live' ? 'LIVE' : 'PAPER'}</span>
        <button class="iconBtn soloBtn" data-i="${i}" title="show only this strategy">${solo ? "*" : "1"}</button>
        ${(s.qty||0) ? `<span class="posTag">${s.qty} @ ${fmtPx(s.entry_px)}</span>` : ""}
        ${s.entry || s.target || s.stop
          ? `<span class="dim">${[s.entry?s.entry:"",s.target?"T"+s.target:"",s.stop?"S"+s.stop:""].filter(Boolean).join(" ")}</span>` : ""}
      </div>`;
    }).join("");
    host.querySelectorAll(".sw").forEach(el => {
      el.addEventListener("click", () => {
        const i = +el.dataset.i;
        const s = this.roster[i];
        if (!s) return;
        this.strategyOn[s.label] = !this.strategyOn[s.label];
        this.renderRoster();
        this.applyStrategyMarks();
      });
    });
    host.querySelectorAll(".soloBtn").forEach(el => {
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const i = +el.dataset.i;
        const s = this.roster[i];
        if (!s) return;
        if (this.strategySolo === s.label) {
          this.strategySolo = null;
          for (const r of this.roster) this.strategyOn[r.label] = true;
        } else {
          this.strategySolo = s.label;
          // show only this one
          for (const r of this.roster) this.strategyOn[r.label] = (r.label === s.label);
        }
        this.renderRoster();
        this.applyStrategyMarks();
      });
    });
  }

  applyStrategyMarks() {
    // strategy marks are bounded line SEGMENTS (drawBoundedLevel line series),
    // not price lines - remove them as series
    for (const lbl in this.strategyLines)
      this.strategyLines[lbl].forEach(ser => {
        try { this.chart.removeSeries(ser); } catch {}
      });
    this.strategyLines = {};
    this.strategyLabels = [];
    for (const s of this.roster) {
      if (this.strategyOn[s.label] !== false) this.drawStrategy(s);
    }
    this.renderLevelLabels();
  }

  /* Indicator show/hide. Client-side only: data is still stored, toggling
   * just re-renders (or removes) the corresponding series/lines. */
  renderIndicatorsList() {
    const host = document.getElementById("indicators");
    if (!host) return;
    const ids = Object.keys(this.indicators).concat(
      this.gamma ? ["__gamma__"] : [],
      (this.zones && this.zones.length) ? ["__zones__"] : [],
      (this.strategyLabels && this.strategyLabels.length) ? ["__strategies__"] : [],
      (this.positionLabels && this.positionLabels.length) ? ["__positions__"] : [],
      ["__sessions__"]
    );
    host.innerHTML = ids.map(id => {
      const on = this.indicatorOn[id] !== false;
      let lbl = id;
      if (this.indicators[id]) lbl = this.indicators[id].label || id;
      else if (id === "__gamma__") lbl = "gamma walls";
      else if (id === "__zones__") lbl = "zones";
      else if (id === "__strategies__") lbl = "strategy levels";
      else if (id === "__positions__") lbl = "position levels";
      else if (id === "__sessions__") lbl = "session shading";
      return `<div class="rosterRow ${on ? "" : "off"}">
        <span class="sw ${on ? "on" : ""}" data-id="${escapeHtml(id)}"></span>
        <span class="name">${escapeHtml(lbl)}</span>
      </div>`;
    }).join("");
    host.querySelectorAll(".sw").forEach(el => {
      el.addEventListener("click", () => {
        const id = el.dataset.id;
        this.indicatorOn[id] = !(this.indicatorOn[id] !== false);
        this.renderIndicatorsList();
        if (id === "__gamma__") {
          if (this.indicatorOn[id] === false) {
            for (const ser of (this.gammaLines || [])) try { this.chart.removeSeries(ser); } catch {}
            this.gammaLines = [];
            this.gammaLabels = [];
            this.applyMarkers();
            this.renderLevelLabels();
          } else if (this.gamma) this.renderGamma(this.gamma);
        } else if (id === "__zones__") {
          if (this.indicatorOn[id] === false) {
            this.drawZoneRects();
          } else this.drawZones();
        } else if (id === "__sessions__") {
          this.drawSessionShading();
        } else if (id === "__strategies__") {
          if (this.indicatorOn[id] === false) {
            for (const lbl of (this.strategyLabels || [])) {} // keep data
            // remove strategy lines
            for (const k in this.strategyLines) {
              for (const ser of (this.strategyLines[k] || [])) try { this.chart.removeSeries(ser); } catch {}
            }
            this.strategyLines = {};
            this.strategyLabels = [];
            this.renderLevelLabels();
          } else {
            this.applyStrategyMarks();
          }
        } else if (id === "__positions__") {
          if (this.indicatorOn[id] === false) {
            for (const ser of (this.positionLines || [])) try { this.chart.removeSeries(ser); } catch {}
            this.positionLines = [];
            this.positionLabels = [];
            this.renderLevelLabels();
          } else {
            this.drawPositions();
          }
        } else {
          const ind = this.indicators[id];
          if (ind) this.upsertIndicator(ind, false);
        }
      });
    });
  }

  drawStrategy(s) {
    const lines = [];
    const labels = [];
    // engine timestamps are epoch-NANOSECONDS -> convert once, here
    const st = s.start_ts != null ? sec(s.start_ts) : null;
    const et = s.end_ts != null ? sec(s.end_ts) : null;
    const add = (price, color, style, label) => {
      if (price == null) return;
      const ser = this.drawBoundedLevel(price, st, et, color, style, 1);
      if (ser) lines.push(ser);
      labels.push({ price, color, text: label });
    };
    if (s.entry) add(s.entry, "#4f9cf9", 0, s.label + " entry");
    if (s.target) add(s.target, "#26a69a", 2, s.label + " target");
    if (s.stop) add(s.stop, "#ef5350", 2, s.label + " stop");
    this.strategyLines[s.label] = lines;
    this.strategyLabels = (this.strategyLabels || []).concat(labels);
  }

  setZones(list) {
    this.zones = list || [];
    if (this.indicatorOn["__zones__"] === false) {
      this.drawZoneRects();
      return;
    }
    this.drawZones();
  }
  drawZones() {
    for (const ser of this.zoneLines) { try { this.chart.removeSeries(ser); } catch {} }
    this.zoneLines = [];
    this.drawZoneRects();
  }

  /* Filled translucent rectangles for zones (canvas overlay). */
  drawZoneRects() {
    const canvas = this.zoneCanvas;
    if (!canvas) return;
    const rect = this.chartBox.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(rect.width * dpr);
    canvas.height = Math.floor(rect.height * dpr);
    canvas.style.width = rect.width + "px";
    canvas.style.height = rect.height + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);
    if (this.indicatorOn["__zones__"] === false) return;
    const ts = this.chart.timeScale();
    const ps = this.candleSeries;
    const rng = this.lastSessionRange();
    for (const z of (this.zones || [])) {
      if (z.top == null || z.bot == null) continue;
      const top = Math.min(z.top, z.bot);
      const bot = Math.max(z.top, z.bot);
      const startSec = z.formed_ts != null ? sec(z.formed_ts) : (this.nowTs() - 86400);
      let endSec = z.end_ts != null ? sec(z.end_ts) : this.nowTs();
      if (z.broken && z.broken_ts != null) endSec = sec(z.broken_ts);
      const x1 = ts.timeToCoordinate(startSec);
      const x2 = ts.timeToCoordinate(endSec);
      const y1 = ps.priceToCoordinate(bot);
      const y2 = ps.priceToCoordinate(top);
      if (x1 == null || x2 == null || y1 == null || y2 == null) continue;
      const x = Math.min(x1, x2);
      const w = Math.abs(x2 - x1);
      const y = Math.min(y1, y2);
      const h = Math.abs(y2 - y1);
      if (w < 1 || h < 1) continue;
      const isSupply = (z.direction || 0) < 0;
      const color = z.broken ? "rgba(107,114,128,0.15)"
        : (isSupply ? "rgba(239,83,80,0.18)" : "rgba(38,166,154,0.18)");
      const stroke = z.broken ? "rgba(107,114,128,0.4)"
        : (isSupply ? "rgba(239,83,80,0.6)" : "rgba(38,166,154,0.6)");
      ctx.fillStyle = color;
      ctx.fillRect(x, y, w, h);
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 1;
      ctx.strokeRect(x, y, w, h);
      // zone tf label
      if (z.tf && w > 30 && h > 12) {
        ctx.fillStyle = stroke;
        ctx.font = "9px sans-serif";
        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        ctx.fillText(z.tf, x + 2, y + 2);
      }
    }
  }


  drawSessionShading() {
    const canvas = this.sessionCanvas;
    if (!canvas) return;
    const rect = this.chartBox.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(rect.width * dpr);
    canvas.height = Math.floor(rect.height * dpr);
    canvas.style.width = rect.width + "px";
    canvas.style.height = rect.height + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);
    if (this.indicatorOn["__sessions__"] === false) return;
    const ts = this.chart.timeScale();
    const c = this.candles;
    if (!c.length) return;
    // session windows in ET (approx, UTC-4 EDT)
    const sessions = [
      {name: "LON", startH: 3, startM: 0, endH: 11, endM: 30, color: "rgba(34,197,94,0.06)"},
      {name: "RTH", startH: 9, startM: 30, endH: 16, endM: 0, color: "rgba(79,156,249,0.08)"},
    ];
    // iterate over visible candles and shade
    for (const s of sessions) {
      // find contiguous ranges of candles within this session
      let inSession = false;
      let startX = null;
      for (let i = 0; i < c.length; i++) {
        const d = new Date(c[i].ts / 1e6);
        const etH = (d.getUTCHours() + 20) % 24;
        const etM = d.getUTCMinutes();
        const mins = etH * 60 + etM;
        const startMins = s.startH * 60 + s.startM;
        const endMins = s.endH * 60 + s.endM;
        const inSess = mins >= startMins && mins < endMins;
        const x = ts.timeToCoordinate(sec(c[i].ts));
        if (x == null) continue;
        if (inSess && !inSession) {
          inSession = true;
          startX = x;
        } else if (!inSess && inSession) {
          inSession = false;
          if (startX != null) {
            ctx.fillStyle = s.color;
            ctx.fillRect(startX, 0, x - startX, rect.height);
          }
          startX = null;
        }
      }
      if (inSession && startX != null) {
        const lastX = ts.timeToCoordinate(sec(c[c.length-1].ts));
        if (lastX != null) {
          ctx.fillStyle = s.color;
          ctx.fillRect(startX, 0, lastX - startX + 10, rect.height);
        }
      }
    }
  }

  _indicatorOn(id) {
    return this.indicatorOn[id] !== false;
  }

  setPositions(list) {
    this.positions = list || [];
    this.drawPositions();
  }
  drawPositions() {
    for (const ser of this.positionLines) { try { this.chart.removeSeries(ser); } catch {} }
    this.positionLines = [];
    this.positionLabels = [];
    for (const p of this.positions) {
      const st = p.start_ts != null ? sec(p.start_ts) : null;
      const et = p.end_ts != null ? sec(p.end_ts) : null;
      const add = (pr, color, style, label) => {
        if (pr == null) return;
        const ser = this.drawBoundedLevel(pr, st, et, color, style, 1);
        if (ser) this.positionLines.push(ser);
        this.positionLabels.push({ price: pr, color, text: label });
      };
      if (p.entry != null) add(p.entry, "#4f9cf9", 0, (p.sleeve || "pos") + " entry");
      if (p.target != null) add(p.target, "#089981", 2, (p.sleeve || "pos") + " target");
      if (p.stop != null) add(p.stop, "#f23645", 2, (p.sleeve || "pos") + " stop");
    }
    this.renderLevelLabels();
  }

  addFillMarker(f) {
    if (!f) return;
    const side = (f.size || 0) > 0;
    this.fillMarkers.push({ time: sec(f.ts || Date.now() * 1e6),
      position: side ? "belowBar" : "aboveBar",
      color: side ? "#26a69a" : "#ef5350",
      shape: side ? "arrowUp" : "arrowDown",
      text: (side ? "B " : "S ") + (f.tag || "") });
    this.applyMarkers();
  }
  async loadFills() {
    try {
      const resp = await fetch(`/api/fills?symbol=${encodeURIComponent(this.symbol)}`);
      const data = await resp.json();
      if (!data.fills) return;
      // clear old fill markers
      this.fillMarkers = [];
      for (const f of data.fills) {
        const side = f.side > 0;
        this.fillMarkers.push({
          time: Math.floor(f.ts / 1e9),
          position: side ? "belowBar" : "aboveBar",
          color: side ? "#26a69a" : "#ef5350",
          shape: side ? "arrowUp" : "arrowDown",
          text: (side ? "B " : "S ") + (f.tag || "").slice(0, 12),
        });
      }
      this.applyMarkers();
    } catch (e) {
      console.error("loadFills failed", e);
    }
  }

  addSignalMarker(s) {
    if (!s) return;
    const side = (s.side || 0) > 0;
    const ts = s.ts || Date.now() * 1e6;
    this.signalMarkers.push({
      time: sec(ts),
      position: side ? "belowBar" : "aboveBar",
      color: side ? "#22c55e" : "#ef5350",
      shape: side ? "arrowUp" : "arrowDown",
      text: (s.tag || (side ? "BUY" : "SELL")),
    });
    this.applyMarkers();
  }

  applyMarkers() {
    const markers = this.fillMarkers.slice(-100).concat(this.signalMarkers.slice(-100));
    if (this.sessionMarkers) markers.push(...this.sessionMarkers);
    if (this.pivotMarkers) markers.push(...this.pivotMarkers);
    if (this.gammaMarkers) markers.push(...this.gammaMarkers);
    markers.sort((a, b) => a.time - b.time);
    this.candleSeries.setMarkers(markers.slice(-200));
  }

  computeSessionMarkers() {
    const out = [];
    const seen = new Set();
    for (const c of this.candles) {
      const d = new Date(c.ts / 1e6);
      // ET = UTC-4 (EDT) approx. Good enough for session markers.
      const etHour = (d.getUTCHours() + 20) % 24;
      const etMin = d.getUTCMinutes();
      const dayKey = d.toISOString().slice(0, 10);
      const key = (h, m, label) => `${dayKey}-${label}`;
      // London open 03:00 ET
      if (etHour === 3 && etMin === 0 && !seen.has(key(3,0,"LON"))) {
        seen.add(key(3,0,"LON"));
        out.push({ time: sec(c.ts), position: "aboveBar",
          color: "#22c55e", shape: "text", text: "LON" });
      }
      // Paris open 03:00 ET (approx, CET; same slot, distinct color)
      if (etHour === 3 && etMin === 0 && !seen.has(key(3,0,"PAR"))) {
        seen.add(key(3,0,"PAR"));
        out.push({ time: sec(c.ts), position: "aboveBar",
          color: "#f59e0b", shape: "text", text: "PAR" });
      }
      // RTH open 09:30 ET
      if (etHour === 9 && etMin === 30 && !seen.has(key(9,30,"RTH"))) {
        seen.add(key(9,30,"RTH"));
        out.push({ time: sec(c.ts), position: "aboveBar",
          color: "#4f9cf9", shape: "text", text: "RTH" });
      }
    }
    this.sessionMarkers = out;
  }

  /* ── WebSocket: this window's own socket (origin + tf scoped server-side) */
  connect() {
    this.disconnect();
    const url = `ws://${location.host}/ws?symbol=${encodeURIComponent(this.symbol)}&tf=${encodeURIComponent(this.tf)}`;
    this.ws = new WebSocket(url);
    const self = this;
    // reconnect if no message arrives within 8s (handles connections made during server seeding)
    this._msgTimer = setTimeout(() => {
      if (self.candles.length === 0) {
        console.warn('no data received, reconnecting...');
        self.connect();
      }
    }, 8000);
    this.ws.onopen = () => { this._reconnectDelay = 1500; setStatus("ok", "connected"); };
    this.ws.onmessage = (ev) => {
      if (this._msgTimer) { clearTimeout(this._msgTimer); this._msgTimer = null; }
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      try {
        self.handleMessage(msg);
      } catch (e) {
        console.error('Error handling WS msg', e, msg);
      }
    };
    this.ws.onclose = () => {
      if (self.closed) return;
      setStatus("err", "disconnected — retrying");
      setTimeout(() => self.connect(), self._reconnectDelay);
      self._reconnectDelay = Math.min(self._reconnectDelay * 2, 30000);
    };
    this.ws.onerror = () => setStatus("err", "ws error");
  }

  disconnect() {
    if (this.ws) {
      this.closed = true;
      try { this.ws.close(); } catch {}
      this.ws = null;
    }
  }

  handleMessage(msg) {
    switch (msg.type) {
      case "hello":
        LANES = msg.lanes; TIMEFRAMES = msg.timeframes;
        this.symbol = msg.symbol || this.symbol;
        this.tf = msg.tf || this.tf;
        populateSelects();
        this.updateHeader();
        break;
      case "__snapshot__":
        // server relays the subscriber's initial snapshot under this type
        this.applySnapshot({
          type: "snapshot",
          payload: msg.payload && msg.payload.lanes ? msg.payload.lanes[this.symbol] : msg.payload,
          log: msg.log || [], errors: msg.errors || [],
        });
        break;
      case "snapshot":
        this.applySnapshot(msg); break;
      case "__post_seed__":
        this.applySnapshot({type: "snapshot", payload: msg.payload.lanes[this.symbol] ? {bars: msg.payload.lanes[this.symbol].bars_raw, indicators: msg.payload.lanes[this.symbol].indicators, gamma: msg.payload.lanes[this.symbol].gamma, strategies_roster: msg.payload.lanes[this.symbol].strategies_roster, zones: [], positions: []} : {}, log: msg.payload.log || [], errors: msg.payload.errors || []});
        break;
      case "delta": this.applyDelta(msg); break;
      case "delta_batch": this.applyDeltaBatch(msg); break;
    }
  }

  /* Batched deltas (server flushed a burst). Two phases: FIRST mutate the
   * model for every delta in the batch (bars collected into pending arrays,
   * indicators merged, overlays applied), THEN run a single render pass —
   * one series.update() per bar (LWC coalesces these into one repaint per
   * animation frame) and one marker refresh. A 50-bar burst therefore costs
   * one WS frame, one JSON parse and one repaint, not 50 of each. */
  applyDeltaBatch(msg) {
    const deltas = msg.deltas || [];
    if (!deltas.length) return;
    const barData = [], volData = [];
    let sawBar = false, sawFill = false;
    // LWC rejects a bar older than the series' last one (see updateBar): the
    // batch path needs the same guard, tracked across the batch.
    let lastBarTs = this.candles.length
      ? this.candles[this.candles.length - 1].ts : null;
    for (const d of deltas) {
      if (!d || !d.kind) continue;
      const pl = d.payload || {};
      switch (d.kind) {
        case "bar": {
          const b = pl.bar;
          if (!b || typeof b.ts !== 'number' || typeof b.o !== 'number' ||
              typeof b.h !== 'number' || typeof b.l !== 'number' ||
              typeof b.c !== 'number') break;
          if (lastBarTs !== null && b.ts < lastBarTs) break;   // out of order
          const last = this.candles.length ? this.candles[this.candles.length - 1] : null;
          if (last && last.ts === b.ts) this.candles[this.candles.length - 1] = b;
          else this.candles.push(b);
          lastBarTs = b.ts;
          barData.push({ time: sec(b.ts), open: b.o, high: b.h, low: b.l, close: b.c });
          volData.push({ time: sec(b.ts),
            value: (typeof b.v === 'number' && !isNaN(b.v)) ? b.v : 0,
            color: b.o <= b.c ? "rgba(38,166,154,0.55)" : "rgba(239,83,80,0.55)" });
          sawBar = true;
          break;
        }
        case "indicator": this.upsertIndicator(pl, true); break;
        case "gamma": this.renderGamma(pl.gamma); break;
        case "roster": this.setRoster(pl.roster); break;
        case "zone": this.setZones(zonesFromDelta(this.zones, pl)); break;
        case "position": this.applyPositionDelta(pl.position); break;
        case "fill": this.addFillMarker(pl); sawFill = true; break;
        case "signal": this.addSignalMarker(pl); sawFill = true; break;
        case "log": appendLog(pl, false); break;
        case "error": appendLog(pl, true); break;
      }
    }
    if (sawBar) {
      for (const p of barData) this.candleSeries.update(p);
      for (const p of volData) this.volSeries.update(p);
      this.followEnd();
    }
    if (sawFill) this.applyMarkers();
  }

  /* Keep the view pinned to the newest bars.
   *
   * LWC's own "shiftVisibleRangeOnNewBar" keeps the visible TIME window
   * fixed, so as the tape grows the chart drifts LEFT off the data and the
   * newest bars scroll out of sight. We re-anchor on every new bar instead.
   *
   * The anchor is expressed in TIME, not logical indices: the logical index
   * space is the union of every series' timestamps, which shifts whenever an
   * overlay series is added or removed, so an index-based anchor silently
   * lands in the wrong place. A user who deliberately scrolled back is left
   * alone — we only re-anchor when the view is still parked at the right edge.
   */
  followEnd() {
    if (this._autoScroll === false) return;
    const ts = this.chart.timeScale();
    const c = this.candles;
    if (!c.length) return;
    const last = c[c.length - 1];
    const prev = c.length > 1 ? c[c.length - 2] : last;
    const step = Math.max(1, (last.ts - prev.ts) / 1e9);
    const lastT = sec(last.ts);
    let vr = null;
    try { vr = ts.getVisibleRange(); } catch { return; }
    if (!vr) return;
    // the user scrolled away from the right edge -> stop following
    if (vr.to < lastT - step * 2) { this._autoScroll = false; this.updateAutoScrollBtn(); return; }
    this._following = true;
    const span = Math.max(step, vr.to - vr.from);
    const to = lastT + step * 3;             // rightOffset worth of padding
    try { ts.setVisibleRange({ from: to - span, to }); } catch {}
  }

  /* Scroll-back: load older bars when the view reaches the left edge. */
  maybeLoadOlder() {
    if (this._loadingHistory || this._noMoreHistory) return;
    if (!this.candles.length) return;
    const ts = this.chart.timeScale();
    let vr = null;
    try { vr = ts.getVisibleRange(); } catch { return; }
    if (!vr) return;
    const firstCandleSec = sec(this.candles[0].ts);
    // trigger when view's left edge is within 2 bar-spans of the first candle
    const step = this.gridStep();
    if (vr.from > firstCandleSec + step * 2) return;
    this.loadOlder();
  }

  async loadOlder() {
    if (this._loadingHistory) return;
    this._loadingHistory = true;
    const firstTs = this.candles.length ? this.candles[0].ts : null;
    if (!firstTs) { this._loadingHistory = false; return; }
    const symbol = this.symbol;
    const tf = this.tf;
    const endSec = Math.floor(firstTs / 1e9);
    const url = `/history?symbol=${encodeURIComponent(symbol)}&tf=${encodeURIComponent(tf)}&end=${endSec}&limit=2000`;
    try {
      const resp = await fetch(url);
      if (!resp.ok) { this._loadingHistory = false; return; }
      const data = await resp.json();
      const newBars = (data.bars && (data.bars[tf] || data.bars)) || [];
      if (!Array.isArray(newBars) || !newBars.length) { this._noMoreHistory = true; this._loadingHistory = false; return; }
      // merge: prepend older bars, dedup by ts
      const seen = new Set(this.candles.map(c => c.ts));
      const older = newBars.filter(b => !seen.has(b.ts));
      if (!older.length) { this._noMoreHistory = true; this._loadingHistory = false; return; }
      // sort older ascending
      older.sort((a, b) => a.ts - b.ts);
      this.candles = older.concat(this.candles);
      // re-render candles
      const cd = this.candles.map(b => ({ time: sec(b.ts), open: b.o, high: b.h, low: b.l, close: b.c }));
      this.candleSeries.setData(cd);
      const vd = this.candles.map(b => ({ time: sec(b.ts), value: (typeof b.v === 'number' && !isNaN(b.v)) ? b.v : 0, color: b.o <= b.c ? "rgba(38,166,154,0.55)" : "rgba(239,83,80,0.55)" }));
      this.volSeries.setData(vd);
      // merge VWAP points from history (snap to candle grid)
      const newInds = data.indicators || [];
      for (const ind of newInds) {
        if (ind.id === 'vwap' || ind.id === 'VWAP') {
          const pts = this.snapToGrid(ind.points || []);
          const cur = this.indicators['vwap'];
          if (cur) {
            const idx = new Map(cur.points.map((p, i) => [p.ts, i]));
            for (const p of pts) {
              const i = idx.get(p.ts);
              if (i >= 0) cur.points[i] = p;
              else { cur.points.push(p); idx.set(p.ts, cur.points.length - 1); }
            }
            cur.points.sort((a, b) => a.ts - b.ts);
            this.vwapSeries.setData(cur.points.filter(p => typeof p.v === 'number' && !isNaN(p.v)).map(p => ({ time: sec(p.ts), value: p.v })));
            // also update band series if present (skip for now - bands not in /history)
          }
        }
      }
      this._loadingHistory = false;
    } catch (e) {
      console.error('loadOlder failed', e);
      this._loadingHistory = false;
    }
  }

  goToLatest() {
    this._autoScroll = true;
    this.updateAutoScrollBtn();
    const ts = this.chart.timeScale();
    const c = this.candles;
    if (!c.length) return;
    const last = c[c.length - 1];
    const prev = c.length > 1 ? c[c.length - 2] : last;
    const step = Math.max(1, (last.ts - prev.ts) / 1e9);
    const lastT = sec(last.ts);
    let vr = null;
    try { vr = ts.getVisibleRange(); } catch { return; }
    const span = vr ? Math.max(step, vr.to - vr.from) : step * 60;
    const to = lastT + step * 3;
    try { ts.setVisibleRange({ from: to - span, to }); } catch {}
  }

  updateAutoScrollBtn() {
    const btn = document.getElementById("latestBtn");
    if (!btn) return;
    btn.textContent = this._autoScroll ? "auto" : "latest";
    btn.style.color = this._autoScroll ? "" : "#e2a03f";
  }

  /* Full state reconciliation for a fresh snapshot (first connect, or a
   * reconnect where the server re-sends the whole picture): tear down every
   * stale overlay — price lines, candle/indicator marker state, accumulated
   * series, roster/gamma/zones/positions — so nothing from a previous session
   * or a broken socket survives into the new picture. Call BEFORE re-rendering
   * the snapshot payload. */
  resetForSnapshot() {
    // gamma walls are LWC price lines; every other overlay is a SERIES.
    // Passing a series to removePriceLine() throws "t._w is not a function",
    // which aborted this method (and the whole snapshot) mid-way.
    const clearPriceLines = (arr) => {
      if (!arr) return;
      for (const pl of arr) {
        try { this.candleSeries && this.candleSeries.removePriceLine(pl); } catch {}
      }
    };
    const clearSeries = (arr) => {
      if (!arr) return;
      for (const ser of arr) { try { this.chart.removeSeries(ser); } catch {} }
    };
    for (const ser of (this.gammaLines || [])) try { this.chart.removeSeries(ser); } catch {}
    this.gammaLines = [];
    this.gammaLabels = [];
    this.strategyLabels = [];
    this.positionLabels = [];
    for (const ser of (this.pivotLinesMulti || [])) try { this.chart.removeSeries(ser); } catch {}
    for (const ser of (this.pivotLinesDaily || [])) try { this.chart.removeSeries(ser); } catch {}
    this.pivotLinesMulti = [];
    this.pivotLinesDaily = [];
    clearSeries(this.staticLines);    this.staticLines = [];
    clearSeries(this.zoneLines);      this.zoneLines = [];
    this.drawZoneRects();
    for (const lbl in this.strategyLines)
      clearSeries(this.strategyLines[lbl]);
    this.strategyLines = {};
    clearSeries(this.positionLines);  this.positionLines = [];
    for (const id in this.indSeries) {
      try { this.chart.removeSeries(this.indSeries[id]); } catch {}
    }
    this.indSeries = {};
    this.fillMarkers = [];
    if (this.candleSeries) this.candleSeries.setMarkers([]);
    if (this.vwapSeries) this.vwapSeries.setData([]);
    for (const k in this.bandSeries) if (this.bandSeries[k]) this.bandSeries[k].setData([]);
    for (const a of this.bandAreas) { try { a.ser.setData([]); } catch {} }
    // accumulated data + state structures (racked fresh)
    this.candles = [];
    this.indicators = {};
    this.gamma = null;
    this.roster = [];
    this.strategyOn = {};
    this.zones = [];
    this.positions = [];
  }

  applySnapshot(msg) {
    const p = msg.payload || {};
    this.resetForSnapshot();
    this.setCandles(p.bars || []);
    for (const ind of p.indicators || []) { try { this.upsertIndicatorSnapshot(ind); } catch (e) { console.error("upsertIndicator failed", ind.id, e); } }
    try { this.renderGamma(p.gamma); } catch (e) { console.error("renderGamma failed", e); }
    try { this.setRoster(p.strategies_roster || []); } catch (e) { console.error("setRoster failed", e); }
    try { this.setZones(p.zones || []); } catch (e) { console.error("setZones failed", e); }
    try { this.setPositions(p.positions || []); } catch (e) { console.error("setPositions failed", e); }
    const self = this;
    try { this.drawSessionShading(); } catch(e) { console.error("drawSessionShading failed", e); }
    try { this.renderLevelLabels(); } catch(e) { console.error("renderLevelLabels failed", e); }
    setTimeout(() => self.renderLevelLabels(), 500);
    for (const s of (p.signals || [])) { try { this.addSignalMarker(s); } catch(e) {} }
    this.loadFills();
    for (const rec of (msg.log || [])) appendLog(rec, false);
    for (const rec of (msg.errors || [])) appendLog(rec, true);
    this.updateHeader();
    this.reflow();
    // initial zoom based ONLY on candles (never on indicators/levels)
    if (!this._initialLoadDone) {
      this._initialLoadDone = true;
      try {
        const cd = this.candles.map(b => ({ time: sec(b.ts) }));
        const dataLen = cd.length;
        const viewSize = 300;
        if (dataLen > viewSize) {
          const step = this.gridStep();
          const lastT = cd[cd.length - 1].time;
          const to = lastT + step * 3;
          this.chart.timeScale().setVisibleRange({
            from: to - viewSize * step, to,
          });
        } else if (dataLen) {
          this.chart.timeScale().setVisibleRange({
            from: cd[0].time,
            to: cd[cd.length - 1].time + 60,
          });
        }
      } catch (e) {}
    }
  }

  /* snapshot indicators carry the FULL series -> non-append replace */
  upsertIndicatorSnapshot(ind) {
    this.upsertIndicator(ind, false);
  }

  applyDelta(msg) {
    const pl = msg.payload || {};
    switch (msg.kind) {
      case "bar": this.updateBar(pl.bar); break;
      case "indicator": this.upsertIndicator(pl, true); break;
      case "gamma": this.renderGamma(pl.gamma); break;
      case "roster": this.setRoster(pl.roster); break;
      case "zone": this.setZones(zonesFromDelta(this.zones, pl)); break;
      case "position": this.applyPositionDelta(pl.position); break;
      case "fill": this.addFillMarker(pl); break;
      case "signal": this.addSignalMarker(pl); break;
      case "log": appendLog(pl, false); break;
      case "error": appendLog(pl, true); break;
    }
  }

  applyPositionDelta(pos) {
    if (pos) {
      const i = this.positions.findIndex(x => x.sleeve === pos.sleeve);
      if (i >= 0) this.positions[i] = pos; else this.positions.push(pos);
    }
    this.drawPositions();
  }

  updateHeader() {
    this.setRegime(this.gamma ? this.gamma.local_sign : null);
  }

  close() {
    this.closed = true;
    this.disconnect();
    this.chart && this.chart.remove();
    this.wrapper && this.wrapper.remove();
    windows.delete(this.id);
  }
}

/* ══════════════════════ page-level wiring ══════════════════════ */
function setStatus(cls, text) {
  const st = $("status");
  st.textContent = text;
  st.className = "status " + cls;
}

function populateSelects() {
  const ls = $("laneSel"); ls.innerHTML = "";
  for (const l of LANES) {
    const o = document.createElement("option"); o.value = l; o.textContent = l;
    if (l === $("laneSel").value || (windows.size && [...windows.values()][0].symbol === l))
      o.selected = true;
    ls.appendChild(o);
  }
  const ts = $("tfSel"); ts.innerHTML = "";
  for (const t of TIMEFRAMES) {
    const o = document.createElement("option"); o.value = t; o.textContent = t;
    if (t === ([...windows.values()][0]?.tf || "5m")) o.selected = true;
    ts.appendChild(o);
  }
}

function openWindowTab(symbol, tf) {
  const u = new URL(location.href);
  u.searchParams.set("symbol", symbol); u.searchParams.set("tf", tf);
  u.hash = "";
  window.open(u.toString(), "_blank");
}

function addWindow(symbol, tf) {
  const container = $("grid");
  const w = new ChartWindow("w" + (++windowCounter), symbol, tf, container);
  windows.set(w.id, w);
  window.__chartWin = w;   // debug/testing hook
  w.connect();
  updateGrid();
  return w;
}

function updateGrid() {
  const n = windows.size;
  document.body.classList.toggle("grid2up", n > 1);
  [...windows.values()].forEach((w, i) => {
    w.closeBtn.classList.toggle("hidden", n <= 1);
  });
  requestAnimationFrame(() => [...windows.values()].forEach(w => w.reflow()));
}

function toggleGrid() {
  // add a second window inline (grid) if none exists yet
  if (windows.size < 2) {
    const sym = LANES.find(l => ![...windows.values()].some(w => w.symbol === l)) || LANES[0];
    addWindow(sym, TIMEFRAMES[0]);
  } else {
    // collapse back to a single window (close extra ones)
    const list = [...windows.values()];
    for (let i = 1; i < list.length; i++) { list[i].close(); }
  }
  updateGrid();
}

const zonesFromDelta = (cur, pl) => {
  const m = {}; for (const z of cur) m[z.id] = z;
  for (const rm of pl.removed || []) delete m[rm];
  for (const z of pl.added || []) m[z.id] = z;
  for (const z of pl.updated || []) m[z.id] = z;
  return Object.values(m);
};

function renderGammaDrawerFor(w) {
  const host = $("gammaBody");
  if (!host) return;
  const g = w.gamma;
  if (!g || !g.ok) { host.textContent = "—"; return; }
  const regime = g.local_sign > 0 ? "LONG γ" : g.local_sign < 0 ? "SHORT γ" : "flat";
  const lines = [];
  lines.push(`<div class="gammaRegime ${g.local_sign>0?"long":g.local_sign<0?"short":""}">
    <span class="kind">regime</span><span class="px">${regime}</span>
    <span class="gex">@ ${fmtPx(g.price)}</span></div>`);
  const row = (kind, color, ks) => ks.filter(k => k != null).map(k =>
    `<div class="gammaRow"><span class="swatch" style="background:${color}"></span>
     <span class="kind">${kind}</span><span class="px">${fmtPx(k)}</span></div>`).join("");
  lines.push(`<div class="gammaGroup">flips</div>` + row("flip", "#e6a9ff",
    (g.flips||[]).map(f => Array.isArray(f) ? f[0] : f)));
  lines.push(`<div class="gammaGroup">call walls <span class="hint">(ranked by size)</span></div>` +
    row("call", "#ffa500", (g.call_walls||[]).map(w => Array.isArray(w) ? w[0] : w)));
  lines.push(`<div class="gammaGroup">put walls <span class="hint">(ranked by size)</span></div>` +
    row("put", "#1e90ff", (g.put_walls||[]).map(w => Array.isArray(w) ? w[0] : w)));
  if (g.pocket && (g.pocket[0] != null || g.pocket[1] != null))
    lines.push(`<div class="gammaRow"><span class="kind">pocket</span>
      <span class="px">${fmtPx(g.pocket[0])} → ${fmtPx(g.pocket[1])}</span></div>`);
  host.innerHTML = lines.join("");
}

/* ── log drawer + error badge ────────────────────────────────────────── */
const LOG_MAX = 300;
function appendLog(rec, isError) {
  const host = $("log");
  if (!host || !rec) return;
  const line = document.createElement("div");
  const d = new Date(rec.ts ? rec.ts / 1e6 : Date.now());
  line.className = "logline " + (rec.level || "").toLowerCase();
  line.innerHTML = `<span class="t">${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}</span>` +
    `<span class="msg">${escapeHtml(rec.level || "")} ${escapeHtml(rec.message || "")}</span>`;
  host.appendChild(line);
  while (host.children.length > LOG_MAX) host.removeChild(host.firstChild);
  if (isError || rec.level === "ERROR" || rec.level === "CRITICAL") {
    const badge = $("errorBadge");
    badge.classList.remove("hidden");
    badge.textContent = (parseInt(badge.textContent || "0", 10) + 1);
    setStatus("err", "warnings present");
  }
}

function toggleDrawer(id) { $(id).classList.toggle("hidden"); }

/* ── topbar button wiring ────────────────────────────────────────────── */
$("plusBtn").addEventListener("click", () => {
  const sym = LANES[0], tf = ([...windows.values()][0]?.tf) || "5m";
  openWindowTab(sym, tf);                  // new browser tab = new chart window
});
$("popBtn").addEventListener("click", () => {
  const w = [...windows.values()][0];
  if (w) openWindowTab(w.symbol, w.tf);
});
$("gridBtn").addEventListener("click", toggleGrid);
$("latestBtn").addEventListener("click", () => {
  const w = [...windows.values()][0];
  if (w) w.goToLatest();
});
$("stratBtn").addEventListener("click", () => {
  toggleDrawer("stratDrawer");
  const w = [...windows.values()][0]; if (w) w.renderRoster();
});
$("gammaBtn").addEventListener("click", () => {
  toggleDrawer("gammaDrawer");
  const w = [...windows.values()][0]; if (w) renderGammaDrawerFor(w);
});
$("indBtn").addEventListener("click", () => {
  toggleDrawer("indDrawer");
  const w = [...windows.values()][0]; if (w) w.renderIndicatorsList();
});
$("laneSel").addEventListener("change", (ev) => {
  const w = [...windows.values()][0];
  if (w && w.symbol !== ev.target.value) {
    w.symbol = ev.target.value; w.connect(); w.updateHeader();
  }
});
$("tfSel").addEventListener("change", (ev) => {
  const w = [...windows.values()][0];
  if (w && w.tf !== ev.target.value) {
    w.tf = ev.target.value; w.connect(); w.updateHeader();
  }
});

/* ── boot: one chart-first window from the URL, chart fills the screen ─ */
window.addEventListener("resize", () =>
  requestAnimationFrame(() => [...windows.values()].forEach(w => w.reflow())));
window.addEventListener("pagehide", () => {
  for (const w of [...windows.values()]) w.close();
});
populateSelects();
addWindow(qs.get("symbol") || "ES", qs.get("tf") || "5m");
