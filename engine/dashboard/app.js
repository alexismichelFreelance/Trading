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

    // this window's own band overlay canvas
    this.canvas = document.createElement("canvas");
    this.canvas.className = "bandCanvas";
    this.chartBox.appendChild(this.canvas);
    this.ctx = null;

    this.closeBtn.addEventListener("click", () => this.close());
    this.popBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      openWindowTab(this.symbol, this.tf);
    });
    container.appendChild(this.wrapper);
  }

  buildCharts() {
    const opts = (el) => ({
      layout: { background: { type: "solid", color: "#0b0e14" },
                textColor: "#dbe2ea", fontSize: 11 },
      grid: { vertLines: { color: "#161c26" }, horzLines: { color: "#161c26" } },
      crosshair: { mode: 0 },   // Normal
      timeScale: { borderColor: "#232c3b", timeVisible: true,
                   secondsVisible: false, rightOffset: 3 },
      rightPriceScale: { borderColor: "#232c3b" },
      autoSize: false,
    });
    this.chart = LightweightCharts.createChart(this.chartBox, opts());
    this.volChart = LightweightCharts.createChart(this.volBox, {
      ...opts(), layout: { background: { type: "solid", color: "#0b0e14" },
                           textColor: "#8494a8", fontSize: 10 },
      rightPriceScale: { visible: false, borderColor: "#232c3b" },
      timeScale: { visible: true, timeVisible: true,
                   secondsVisible: false, borderColor: "#232c3b" },
      crosshair: { mode: 0 },
    });
    // anchor the volume chart's time axis to the price chart's
    this.volChart.timeScale().applyOptions({
      rightOffset: 3,
    });
  }

  buildSeries() {
    this.candleSeries = this.chart.addCandlestickSeries({
      upColor: "#26a69a", downColor: "#ef5350",
      borderUpColor: "#26a69a", borderDownColor: "#ef5350",
      wickUpColor: "#4db6ac", wickDownColor: "#ef9a9a",
      priceLineVisible: false,
    });

    // VWAP curve + band edge line series (all on the main chart's scale)
    this.vwapSeries = this.chart.addLineSeries({
      color: "#e2a03f", lineWidth: 2, priceLineVisible: false,
      lastValueVisible: true, crosshairMarkerVisible: false,
    });
    const bandSpec = {
      vwap_2s_hi: { color: "#f0b64a", lineWidth: 1 },   // +2σ golden
      vwap_2s_lo: { color: "#f0b64a", lineWidth: 1 },   // -2σ
      vwap_3s_hi: { color: "#e08b2e", lineWidth: 1, lineStyle: 2 }, // dashed +3σ
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

    this.volSeries = this.volChart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "", scaleMargins: { top: 0.7, bottom: 0 },
    });
  }

  reflow() {
    const r = (el) => el.getBoundingClientRect();
    this.chart.applyOptions({ width: Math.max(200, Math.floor(r(this.chartBox).width)),
                              height: Math.max(120, Math.floor(r(this.chartBox).height)) });
    this.volChart.applyOptions({ width: Math.max(200, Math.floor(r(this.volBox).width)),
                                 height: Math.max(60, Math.floor(r(this.volBox).height)) });
    // keep canvas overlay in step
    const cr = r(this.chartBox);
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.max(1, Math.floor(cr.width * dpr));
    this.canvas.height = Math.max(1, Math.floor(cr.height * dpr));
    this.canvas.style.width = cr.width + "px";
    this.canvas.style.height = cr.height + "px";
    this.ctx = this.canvas.getContext("2d");
    this.drawOverlay();
    this.chart.timeScale().subscribeVisibleLogicalRangeChange(() => this.drawOverlay());
    this.chart.timeScale().subscribeVisibleTimeRangeChange(() => this.drawOverlay());
  }

  /* ── data ──────────────────────────────────────────────────────────── */
  setCandles(list) {
    this.candles = list || [];
    const cd = this.candles.map(b => ({ time: sec(b.ts), open: b.o, high: b.h,
                                       low: b.l, close: b.c }));
    this.candleSeries.setData(cd);
    const vd = this.candles.map(b => ({ time: sec(b.ts), value: b.v,
      color: b.o <= b.c ? "rgba(38,166,154,0.55)" : "rgba(239,83,80,0.55)" }));
    this.volSeries.setData(vd);
    this.applyMarkers();
  }

  updateBar(b) {
    if (!b) return;
    const last = this.candles.length ? this.candles[this.candles.length - 1] : null;
    const datum = { time: sec(b.ts), open: b.o, high: b.h, low: b.l, close: b.c };
    const vdat = { time: sec(b.ts), value: b.v,
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

  upsertIndicator(ind) {
    if (!ind || !ind.id || !ind.points) return;
    this.indicators[ind.id] = ind;
    if (ind.id === "vwap" || this.bandSeries[ind.id]) {
      // time series -> line series (vwap curve + band edges)
      const points = ind.points.slice().sort((a, b) => a.ts - b.ts)
        .map(p => ({ time: sec(p.ts), value: p.v }));
      const ser = ind.id === "vwap" ? this.vwapSeries : this.bandSeries[ind.id];
      if (ser) { ser.setData(points); this.drawOverlay(); }
      return;
    }
    if ((ind.producer || "").includes("pivot")) {
      // floor pivots: a fixed intraday grid -> dashed horizontal lines
      this.drawStaticLines(ind);
      return;
    }
    // unknown non-pivot, non-vwap series: draw as a generic line
    let ser = this.indSeries[ind.id];
    if (!ser) {
      ser = this.chart.addLineSeries({ color: "#a78bfa", lineWidth: 1,
        priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false });
      this.indSeries[ind.id] = ser;
    }
    ser.setData(ind.points.slice().sort((a, b) => a.ts - b.ts)
      .map(p => ({ time: sec(p.ts), value: p.v })));
  }

  drawStaticLines(ind) {
    // remove previous lines for this indicator id
    this.staticLines.forEach(pl => this.candleSeries.removePriceLine(pl));
    this.staticLines = [];
    const seen = new Set();
    for (const p of ind.points) {
      const key = p.v.toFixed(4);
      if (seen.has(key)) continue;
      seen.add(key);
      this.staticLines.push(this.candleSeries.createPriceLine({
        price: p.v, color: "#a78bfa", lineWidth: 1,
        lineStyle: 2, axisLabelVisible: true, title: "pivot",
      }));
    }
  }

  /* ── overlay: translucent VWAP sigma-band fills ───────────────────── */
  drawOverlay() {
    const ctx = this.ctx;
    if (!ctx || !this.chart) return;
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const pairs = [["vwap_2s_lo", "vwap_2s_hi", "rgba(226,160,63,0.16)"],
                   ["vwap_3s_lo", "vwap_3s_hi", "rgba(226,160,63,0.09)"]];
    for (const [loId, hiId, fill] of pairs) {
      const lo = this.indicators[loId], hi = this.indicators[hiId];
      if (!lo || !hi || !lo.points || !hi.points) continue;
      const loPts = this.project(lo.points);
      const hiPts = this.project(hi.points);
      if (loPts.length < 2 || hiPts.length < 2) continue;
      ctx.beginPath();
      ctx.moveTo(loPts[0][0], loPts[0][1]);
      for (let i = 1; i < loPts.length; i++) ctx.lineTo(loPts[i][0], loPts[i][1]);
      for (let i = hiPts.length - 1; i >= 0; i--) ctx.lineTo(hiPts[i][0], hiPts[i][1]);
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();
    }
  }

  project(points) {
    const out = [];
    for (const p of points) {
      let x = this.chart.timeScale().timeToCoordinate(sec(p.ts));
      let y = this.candleSeries.priceToCoordinate(p.v);
      if (x == null || y == null) continue;
      out.push([x, y]);
    }
    return out;
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
    for (const lbl in this.strategyLines)
      this.strategyLines[lbl].forEach(pl => this.candleSeries.removePriceLine(pl));
    this.strategyLines = {};
    for (const s of this.roster) {
      if (this.strategyOn[s.label] !== false) this.drawStrategy(s);
    }
  }

  drawStrategy(s) {
    const lines = [];
    const add = (price, color, style, label) => {
      if (price == null) return;
      lines.push(this.candleSeries.createPriceLine({
        price, color, lineWidth: 1, lineStyle: style,
        axisLabelVisible: true, title: label }));
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
    this.staticLines.forEach(pl => this.candleSeries.removePriceLine(pl));
    this.staticLines = [];
    for (const z of this.zones) {
      const color = (z.direction || 0) > 0 ? "#26a69a" : "#ef5350";
      for (const [pr, nm] of [[z.top, "zone-top"], [z.bot, "zone-bot"]]) {
        if (pr == null) continue;
        this.staticLines.push(this.candleSeries.createPriceLine({
          price: pr, color, lineWidth: 1, lineStyle: 1,
          axisLabelVisible: false, title: z.tf + " " + nm }));
      }
    }
  }

  setPositions(list) {
    this.positions = list || [];
    this.drawPositions();
  }
  drawPositions() {
    this.positionLines.forEach(pl => this.candleSeries.removePriceLine(pl));
    this.positionLines = [];
    for (const p of this.positions) {
      if (p.entry) this.positionLines.push(this.candleSeries.createPriceLine({
        price: p.entry, color: "#4f9cf9", lineWidth: 1, lineStyle: 0,
        axisLabelVisible: true, title: (p.sleeve||"pos") + " entry" }));
      if (p.target) this.positionLines.push(this.candleSeries.createPriceLine({
        price: p.target, color: "#26a69a", lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: (p.sleeve||"pos") + " target" }));
      if (p.stop) this.positionLines.push(this.candleSeries.createPriceLine({
        price: p.stop, color: "#ef5350", lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: (p.sleeve||"pos") + " stop" }));
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
    this.ws.onopen = () => setStatus("ok", "connected");
    this.ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      self.handleMessage(msg);
    };
    this.ws.onclose = () => {
      if (self.closed) return;
      setStatus("err", "disconnected — retrying");
      setTimeout(() => self.connect(), 1500);
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
      case "snapshot": this.applySnapshot(msg); break;
      case "delta": this.applyDelta(msg); break;
    }
  }

  applySnapshot(msg) {
    const p = msg.payload || {};
    this.setCandles(p.bars || []);
    for (const ind of p.indicators || []) this.upsertIndicator(ind);
    this.renderGamma(p.gamma);
    this.setRoster(p.strategies_roster || []);
    this.setZones(p.zones || []);
    this.setPositions(p.positions || []);
    for (const rec of (msg.log || [])) appendLog(rec, false);
    for (const rec of (msg.errors || [])) appendLog(rec, true);
    this.updateHeader();
  }

  applyDelta(msg) {
    const pl = msg.payload || {};
    switch (msg.kind) {
      case "bar": this.updateBar(pl.bar); break;
      case "indicator": this.upsertIndicator(pl); break;
      case "gamma": this.renderGamma(pl.gamma); break;
      case "roster": this.setRoster(pl.roster); break;
      case "zone": this.setZones([]); this.setZones(zonesFromDelta(this.zones, pl)); break;
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
    this.volChart && this.volChart.remove();
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
populateSelects();
addWindow(qs.get("symbol") || "ES", qs.get("tf") || "5m");
