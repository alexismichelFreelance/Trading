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
    this.zones = [];            // [{id, tf, top, bot, direction, virgin, touches}]
    this.positions = [];        // [{sleeve, qty, entry, target, stop}]
    this.fillMarkers = [];
    this._logCount = 0;
    this._reconnectDelay = 1500;
    this._initialLoadDone = false;

    // series / line registries
    this.indSeries = {};        // id -> line series (vwap family line series)
    this.staticLines = [];      // pivot/zone price lines on candleSeries
    this.gammaLines = [];
    this.strategyLines = {};    // label -> [priceLine, ...]
    this.positionLines = [];
    this.bandSeries = {};       // id -> line series for vwap hi/lo

    this.buildDom(container);
    this.buildCharts();
    this.buildSeries();
    this.reflow();
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
    const pts = append ? newPts : (src ? src.points : null);
    if (!pts || !pts.length) return;
    const mapped = pts.map(p => ({ time: sec(p.ts), value: p.v }));
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
    this.applyMarkers();

    if (!this._initialLoadDone) {
      this._initialLoadDone = true;
      try {
        const dataLen = cd.length;
        const viewSize = 300;
        if (dataLen > viewSize) {
          this.chart.timeScale().setVisibleLogicalRange({
            from: dataLen - viewSize,
            to: dataLen + 5,
          });
        } else {
          this.chart.timeScale().setVisibleLogicalRange({
            from: 0,
            to: dataLen + 5,
          });
        }
      } catch (e) {}
    }
  }

  updateBar(b) {
    if (!b || typeof b.ts !== 'number' || typeof b.o !== 'number' ||
        typeof b.h !== 'number' || typeof b.l !== 'number' || typeof b.c !== 'number') return;
    const last = this.candles.length ? this.candles[this.candles.length - 1] : null;
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
    } else {
      // snapshot: the payload is the whole series — replace outright
      cur.points = newPts.slice();
      cur.producer = ind.producer || "";
      cur.label = ind.label || "";
      cur.unit = ind.unit || "";
    }
    cur.points.sort((a, b) => a.ts - b.ts);

    if (cur.id === "vwap" || this.bandSeries[cur.id]) {
      const ser = cur.id === "vwap" ? this.vwapSeries : this.bandSeries[cur.id];
      if (!ser) return;
      if (append) {
        for (const p of tail) ser.update({ time: sec(p.ts), value: p.v });
      } else {
        ser.setData(cur.points.map(p => ({ time: sec(p.ts), value: p.v })));
      }
      // band areas are native series now — no canvas overlay to repaint
      this.syncBandAreas(cur.id, append, tail);
      return;
    }
    if ((cur.producer || "").includes("pivot")) {
      this.drawStaticLines(cur);
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
      for (const p of tail) ser.update({ time: sec(p.ts), value: p.v });
    } else {
      ser.setData(cur.points.map(p => ({ time: sec(p.ts), value: p.v })));
    }
  }

  nowTs() {
    const last = this.candles.length ? this.candles[this.candles.length - 1] : null;
    return last ? sec(last.ts) : Math.floor(Date.now() / 1000);
  }

  drawBoundedLevel(price, startTs, endTs, color, lineStyle, lineWidth) {
    if (price == null) return null;
    const now = this.nowTs();
    if (startTs == null) startTs = now - 86400;
    if (endTs == null) endTs = now;
    const series = this.chart.addLineSeries({
      color, lineWidth: lineWidth || 1, lineStyle: lineStyle || 0,
      priceLineVisible: false, lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    series.setData([
      { time: sec(startTs), value: price },
      { time: sec(endTs), value: price },
    ]);
    return series;
  }

  drawStaticLines(ind) {
    for (const ser of this.staticLines) { try { this.chart.removeSeries(ser); } catch {} }
    this.staticLines = [];
    const seen = new Set();
    const now = this.nowTs();
    for (const p of (ind && ind.points) || []) {
      const key = p.v.toFixed(4);
      if (seen.has(key)) continue;
      seen.add(key);
      const ser = this.drawBoundedLevel(p.v, now - 86400, now, "#a78bfa", 2, 1);
      if (ser) this.staticLines.push(ser);
    }
  }

  /* ── dealer-gamma: multi-wall price lines + regime in header ───────── */
  renderGamma(g) {
    this.gamma = g;
    this.gammaLines.forEach(pl => this.candleSeries.removePriceLine(pl));
    this.gammaLines = [];
    if (!g || !g.ok) { this.setRegime(g && g.local_sign); return; }
    const flips = g.flips || [];
    for (const f of flips) {
      this.gammaLines.push(this.candleSeries.createPriceLine({
        price: f, color: "#e6a9ff", lineWidth: 1, lineStyle: 0,
        axisLabelVisible: true, title: "flip" }));
    }
    for (const w of (g.call_walls || [])) {
      const k = Array.isArray(w) ? w[0] : w;
      if (k == null) continue;
      this.gammaLines.push(this.candleSeries.createPriceLine({
        price: k, color: "#ffa500", lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: "call wall" }));
    }
    for (const w of (g.put_walls || [])) {
      const k = Array.isArray(w) ? w[0] : w;
      if (k == null) continue;
      this.gammaLines.push(this.candleSeries.createPriceLine({
        price: k, color: "#1e90ff", lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: "put wall" }));
    }
    this.setRegime(g.local_sign);
    renderGammaDrawerFor(this);   // refresh the shared drawer if it's open
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
      return `<div class="rosterRow ${on ? "" : "off"}">
        <span class="sw ${on ? "on" : ""}" data-i="${i}"></span>
        <span class="name">${escapeHtml(s.label)}</span>
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
  }

  applyStrategyMarks() {
    // strategy marks are bounded line SEGMENTS (drawBoundedLevel line series),
    // not price lines — remove them as series
    for (const lbl in this.strategyLines)
      this.strategyLines[lbl].forEach(ser => {
        try { this.chart.removeSeries(ser); } catch {}
      });
    this.strategyLines = {};
    for (const s of this.roster) {
      if (this.strategyOn[s.label] !== false) this.drawStrategy(s);
    }
  }

  drawStrategy(s) {
    const lines = [];
    const add = (price, color, style, label) => {
      if (price == null) return;
      const ser = this.drawBoundedLevel(price, s.start_ts, s.end_ts, color, style, 1);
      if (ser) lines.push(ser);
    };
    if (s.entry) add(s.entry, "#4f9cf9", 0, s.label + " entry");
    if (s.target) add(s.target, "#26a69a", 2, s.label + " target");
    if (s.stop) add(s.stop, "#ef5350", 2, s.label + " stop");
    this.strategyLines[s.label] = lines;
  }

  setZones(list) {
    this.zones = list || [];
    this.drawZones();
  }
  drawZones() {
    for (const ser of this.staticLines) { try { this.chart.removeSeries(ser); } catch {} }
    this.staticLines = [];
    const now = this.nowTs();
    for (const z of this.zones) {
      const color = (z.direction || 0) > 0 ? "#089981" : "#f23645";
      const startTs = z.formed_ts || (now - 86400);
      const endTs = z.end_ts || (startTs + 86400);
      for (const [pr, nm] of [[z.top, "zone-top"], [z.bot, "zone-bot"]]) {
        if (pr == null) continue;
        const ser = this.drawBoundedLevel(pr, startTs, endTs, color, 1, 1);
        if (ser) this.staticLines.push(ser);
      }
    }
  }

  setPositions(list) {
    this.positions = list || [];
    this.drawPositions();
  }
  drawPositions() {
    for (const ser of this.positionLines) { try { this.chart.removeSeries(ser); } catch {} }
    this.positionLines = [];
    for (const p of this.positions) {
      if (p.entry) {
        const ser = this.drawBoundedLevel(p.entry, p.start_ts, p.end_ts, "#4f9cf9", 0, 1);
        if (ser) this.positionLines.push(ser);
      }
      if (p.target) {
        const ser = this.drawBoundedLevel(p.target, p.start_ts, p.end_ts, "#089981", 2, 1);
        if (ser) this.positionLines.push(ser);
      }
      if (p.stop) {
        const ser = this.drawBoundedLevel(p.stop, p.start_ts, p.end_ts, "#f23645", 2, 1);
        if (ser) this.positionLines.push(ser);
      }
    }
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
  applyMarkers() {
    this.candleSeries.setMarkers(this.fillMarkers.slice(-100));
  }

  /* ── WebSocket: this window's own socket (origin + tf scoped server-side) */
  connect() {
    this.disconnect();
    const url = `ws://${location.host}/ws?symbol=${encodeURIComponent(this.symbol)}&tf=${encodeURIComponent(this.tf)}`;
    this.ws = new WebSocket(url);
    const self = this;
    this.ws.onopen = () => { this._reconnectDelay = 1500; setStatus("ok", "connected"); };
    this.ws.onmessage = (ev) => {
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
    for (const d of deltas) {
      if (!d || !d.kind) continue;
      const pl = d.payload || {};
      switch (d.kind) {
        case "bar": {
          const b = pl.bar;
          if (!b || typeof b.ts !== 'number' || typeof b.o !== 'number' ||
              typeof b.h !== 'number' || typeof b.l !== 'number' ||
              typeof b.c !== 'number') break;
          const last = this.candles.length ? this.candles[this.candles.length - 1] : null;
          if (last && last.ts === b.ts) this.candles[this.candles.length - 1] = b;
          else this.candles.push(b);
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
        case "log": appendLog(pl, false); break;
        case "error": appendLog(pl, true); break;
      }
    }
    if (sawBar) {
      for (const p of barData) this.candleSeries.update(p);
      for (const p of volData) this.volSeries.update(p);
    }
    if (sawFill) this.applyMarkers();
  }

  /* Full state reconciliation for a fresh snapshot (first connect, or a
   * reconnect where the server re-sends the whole picture): tear down every
   * stale overlay — price lines, candle/indicator marker state, accumulated
   * series, roster/gamma/zones/positions — so nothing from a previous session
   * or a broken socket survives into the new picture. Call BEFORE re-rendering
   * the snapshot payload. */
  resetForSnapshot() {
    const clearLines = (arr) => {
      if (!arr) return;
      for (const pl of arr) this.candleSeries && this.candleSeries.removePriceLine(pl);
    };
    clearLines(this.gammaLines);   this.gammaLines = [];
    for (const ser of this.staticLines) {
      try { this.chart.removeSeries(ser); } catch {}
    }
    this.staticLines = [];
    for (const lbl in this.strategyLines)
      this.strategyLines[lbl].forEach(ser => {
        try { this.chart.removeSeries(ser); } catch {}
      });
    this.strategyLines = {};
    clearLines(this.positionLines); this.positionLines = [];
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
    for (const ind of p.indicators || []) this.upsertIndicatorSnapshot(ind);
    this.renderGamma(p.gamma);
    this.setRoster(p.strategies_roster || []);
    this.setZones(p.zones || []);
    this.setPositions(p.positions || []);
    for (const rec of (msg.log || [])) appendLog(rec, false);
    for (const rec of (msg.errors || [])) appendLog(rec, true);
    this.updateHeader();
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
$("stratBtn").addEventListener("click", () => {
  toggleDrawer("stratDrawer");
  const w = [...windows.values()][0]; if (w) w.renderRoster();
});
$("gammaBtn").addEventListener("click", () => {
  toggleDrawer("gammaDrawer");
  const w = [...windows.values()][0]; if (w) renderGammaDrawerFor(w);
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
