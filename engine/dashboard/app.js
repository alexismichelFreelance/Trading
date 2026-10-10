"use strict";

// ── small helpers ────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const qs = new URLSearchParams(location.search);
const sec = (ns) => Math.floor(ns / 1e9);                 // engine ns -> light-charts s
const fmtPx = (x, d) => x == null ? "—" : Number(x).toFixed(d == null ? 2 : d);
const pad = (n) => String(n).padStart(2, "0");
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

let LANES = ["ES"];
let TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];

// ── per-window book keeping ────────────────────────────────────────────────
const windows = new Map();     // id -> ChartWindow
let winCounter = 0;
const nextWinId = () => "win-" + (++winCounter);

/* ════════════════════════════════════════════════════════════════════════
   ChartWindow — one self-contained chart window: its own DOM node, its own
   price chart + volume pane, its own WebSocket. Everything drawn comes from
   the engine's published state (never derived in JS). Managed by the page
   topbar controls (add / pop-out / grid). See index.html for the shell.    */
class ChartWindow {
  constructor(id, symbol, tf) {
    this.id = id;
    this.symbol = symbol;
    this.tf = tf;
    this.candles = [];
    this.indicatorData = {};        // id -> {label, producer, points:[{ts,v}]}
    this.gamma = null;
    this.roster = [];               // strategies_roster entries
    this.zones = [];
    this.positions = [];
    this.strategyOn = {};           // label -> visible (default from roster.enabled)

    // price lines per category so we can remove cleanly
    this.gammaLines = [];
    this.strategyLines = {};        // label -> [priceLine,...]
    this.zoneLines = [];
    this.positionLines = [];

    this.createDom();
    this.createCharts();
    this.createSeries();
    this.updateTitle(null);
    this.parentEl.appendChild(this.wrapperEl);
    this.reflow();
    this.connect();                // own WS per window
  }

  createDom() {
    this.parentEl = $("grid");
    this.wrapperEl = document.createElement("section");
    this.wrapperEl.className = "chartWindowW";
    this.wrapperEl.dataset.wid = this.id;
    this.wrapperEl.innerHTML = `
      <div class="chartWindow">
        <div class="windowHead">
          <span class="winTitle"></span>
          <span class="winSpacer"></span>
          <button class="iconBtn gridPopBtn" title="open this window in its own tab">⤢</button>
          <button class="iconBtn winCloseBtn hidden" title="close this window">✕</button>
        </div>
        <div class="chartCol">
          <div class="chartBox"><canvas class="bandCanvas"></canvas></div>
          <div class="volBox"></div>
        </div>
      </div>`;
    this.chartBoxEl = this.wrapperEl.querySelector(".chartBox");
    this.volEl = this.wrapperEl.querySelector(".volBox");
    this.winTitleEl = this.wrapperEl.querySelector(".winTitle");
    this.popBtn = this.wrapperEl.querySelector(".gridPopBtn");
    this.closeBtn = this.wrapperEl.querySelector(".winCloseBtn");
    this.popBtn.addEventListener("click", () => popOutUrl(this.symbol, this.tf));
    this.closeBtn.addEventListener("click", () => this.destroy());
  }

  createCharts() {
    const [w, h] = this.boxSize(this.chartBoxEl);
    const base = {
      layout: { background: { type: "solid", color: "#0b0e14" },
                textColor: "#dbe2ea" },
      grid: { vertLines: { color: "#1a2230" }, horzLines: { color: "#1a2230" } },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#232c3b" },
      timeScale: { borderColor: "#232c3b", timeVisible: true, secondsVisible: false },
    };
    this.chart = LightweightCharts.createChart(this.chartBoxEl,
      { ...base, width: w, height: h });
    this.volChart = LightweightCharts.createChart(this.volEl, {
      ...base, width: w, height: h,
      layout: { background: { type: "solid", color: "#0b0e14" }, textColor: "#8494a8" },
      rightPriceScale: { borderColor: "#232c3b", visible: false },
      timeScale: { borderColor: "#232c3b", timeVisible: true, secondsVisible: false },
    });
    this.refitTimeScale();
    // redraw the VWAP-band canvas fill whenever the visible window changes
    this.chart.timeScale().subscribeVisibleLogicalRangeChange(() => this.redrawBands());
    this.chart.timeScale().subscribeVisibleTimeRangeChange(() => this.redrawBands());
  }

  createSeries() {
    this.candleSeries = this.chart.addCandlestickSeries({
      upColor: "#26a69a", downColor: "#ef5350",
      borderUpColor: "#26a69a", borderDownColor: "#ef5350",
      wickUpColor: "#8fd0c9", wickDownColor: "#f0a0a0",
    });
    this.volSeries = this.volChart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "", scaleMargins: { top: 0.85, bottom: 0 },
    });
    this.vwapSeries = this.chart.addLineSeries({
      color: "#e2a03f", lineWidth: 2, priceLineVisible: false,
      lastValueVisible: true, title: "VWAP",
    });
    this.bandLines = {};
    this.indSeries = {};
  }

  refitTimeScale() {
    // keep price + volume panes on the same time axis
    try {
      const s = this.chart.timeScale();
      this.volChart.timeScale().applyOptions({
        barSpacing: s.options().barSpacing,
        minBarSpacing: s.options().minBarSpacing,
      });
    } catch (e) { /* ignore */ }
  }

  boxSize(el) {
    const r = el.getBoundingClientRect();
    return [Math.max(200, Math.floor(r.width)), Math.max(120, Math.floor(r.height))];
  }

  reflow() {
    const [w, h] = this.boxSize(this.chartBoxEl);
    this.chart.resize(w, h);
    const [vw, vh] = this.boxSize(this.volEl);
    this.volChart.resize(vw, vh);
    this.refitTimeScale();
    this.fitCanvas();
    this.redrawBands();
  }

  updateTitle(regime) {
    let s = `${this.symbol} · ${this.tf} · live`;
    if (regime === 1) s += ` <span class="regime long">LONG-γ</span>`;
    else if (regime === -1) s += ` <span class="regime short">SHORT-γ</span>`;
    this.winTitleEl.innerHTML = s;
  }

  destroy() {
    if (this.ws) { try { this.ws.close(); } catch (e) {} this.ws = null; }
    this.chart.remove();
    this.volChart.remove();
    this.wrapperEl.remove();
    windows.delete(this.id);
    updateGrid();
  }

  /* ── VWAP bands: draw the band hi/lo as real line series, then fill the
     area between each hi/lo pair on the overlay canvas (lightweight-charts
     has no native "fill between two lines", so we stroke both a line series
     and shade the region — geometry stays correct via the scale mappers).  */
  fitCanvas() {
    const c = this.chartBoxEl.querySelector(".bandCanvas");
    const r = this.chartBoxEl.getBoundingClientRect();
    c.width = r.width * devicePixelRatio;
    c.height = r.height * devicePixelRatio;
    c.style.width = r.width + "px";
    c.style.height = r.height + "px";
    this._ctx = c.getContext("2d");
    this._ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
  }

  toXY(points) {
    // returns [[x, y]] pixel pairs for the points the engine published
    const out = [];
    for (const p of points || []) {
      const x = this.chart.timeScale().timeToCoordinate(sec(p.ts));
      const y = this.candleSeries.priceToCoordinate(p.v);
      if (x == null || y == null) continue;
      out.push([x, y]);
    }
    return out;
  }

  redrawBands() {
    const ctx = this._ctx;
    if (!ctx || !this.chart) return;
    const r = this.chartBoxEl.getBoundingClientRect();
    ctx.clearRect(0, 0, r.width, r.height);
    const pairs = [[["vwap_2s_lo", "vwap_2s_hi"], "rgba(226,160,63,0.22)", "#c08a3f"],
                  [["vwap_3s_lo", "vwap_3s_hi"], "rgba(226,160,63,0.10)", "#8a6a3a"]];
    for (const [[loId, hiId], fill, edge] of pairs) {
      const lo = this.indicatorData[loId], hi = this.indicatorData[hiId];
      if (!lo || !hi || !lo.points.length || !hi.points.length) continue;
      const loXY = this.toXY(lo.points), hiXY = this.toXY(hi.points);
      if (loXY.length < 2 || hiXY.length < 2) continue;
      // filled region between the band's lower and upper curves
      ctx.beginPath();
      ctx.moveTo(loXY[0][0], loXY[0][1]);
      for (let i = 1; i < loXY.length; i++) ctx.lineTo(loXY[i][0], loXY[i][1]);
      for (let i = hiXY.length - 1; i >= 0; i--) ctx.lineTo(hiXY[i][0], hiXY[i][1]);
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();
      // stroke the band edges so the boundaries read clearly
      ctx.strokeStyle = edge;
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (const [x, y] of loXY) { ctx.lineTo(x, y); }
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(hiXY[0][0], hiXY[0][1]);
      for (const [x, y] of hiXY) { ctx.lineTo(x, y); }
      ctx.stroke();
    }
  }

  setBars(list) {
    this.candles = list || [];
    const cd = this.candles.map(b => ({ time: sec(b.ts), open: b.o, high: b.h,
                                       low: b.l, close: b.c }));
    const vd = this.candles.map(b => ({ time: sec(b.ts), value: b.v,
      color: b.o < b.c ? "rgba(38,166,154,0.55)" : "rgba(239,83,80,0.55)" }));
    this.candleSeries.setData(cd);
    this.volSeries.setData(vd);
    // re-anchor the volume pane to the price pane's bars
    this.refitTimeScale();
    this.redrawBands();
  }

  applyBar(b) {
    if (!b) return;
    const last = this.candles.length ? this.candles[this.candles.length - 1] : null;
    if (last && last.ts === b.ts) this.candles[this.candles.length - 1] = b;
    else this.candles.push(b);
    if (this.candles.length > 600) this.candles.shift();
    const d = { time: sec(b.ts), open: b.o, high: b.h, low: b.l, close: b.c };
    this.candleSeries.update(d);
    this.volSeries.update({ time: sec(b.ts), value: b.v,
      color: b.o < b.c ? "rgba(38,166,154,0.55)" : "rgba(239,83,80,0.55)" });
    this.refitTimeScale();
  }

  upsertIndicator(ind) {
    if (!ind || !ind.id || !ind.points || !ind.points.length) return;
    // VWAP family: keep an authoritative line series per series id + band data
    if (ind.id === "vwap") {
      this.vwapSeries.setData(ind.points.map(p => ({ time: sec(p.ts), value: p.v })));
    } else if (ind.id.startsWith("vwap_")) {
      // the sigma-band hi/lo curves are drawn on the canvas overlay
      // (redrawBands) so no per-series bookkeeping / risk of a bad
      // addLineSeries; the engine data is already stored in indicatorData.
      this.redrawBands();
      return;
    }
    // pivots: static intraday grid -> dashed horizontal lines on the candles
    if ((ind.producer || "").includes("pivot")) {
      this.drawStaticLevels(ind);
      return;
    }
    let ser = this.indSeries[ind.id];
    if (!ser) {
      ser = this.chart.addLineSeries({ color: "#a78bfa", lineWidth: 1,
        priceLineVisible: false, lastValueVisible: false, title: ind.label || ind.id });
      this.indSeries[ind.id] = ser;
    }
    ser.setData(ind.points.map(p => ({ time: sec(p.ts), value: p.v }))
                .sort((a, b) => a.time - b.time));
  }

  drawStaticLevels(ind) {
    (ind._lines || []).forEach(pl => this.candleSeries.removePriceLine(pl));
    ind._lines = [];
    const seen = new Set();
    for (const p of ind.points) {
      const key = p.v.toFixed(4);
      if (seen.has(key)) continue; seen.add(key);
      const pl = this.candleSeries.createPriceLine({ price: p.v, color: "#a78bfa",
        lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true, title: ind.label || ind.id });
      ind._lines.push(pl);
    }
  }

  /* ── dealer gamma: multi-walled (put/call), flips, pocket, regime ──── */
  addGammaLine(price, color, style, title) {
    const pl = this.candleSeries.createPriceLine({ price, color, lineWidth: 1,
      lineStyle: style, axisLabelVisible: true, title });
    this.gammaLines.push(pl);
  }

  renderGamma(g) {
    this.gamma = g || null;
    this.gammaLines.forEach(pl => this.candleSeries.removePriceLine(pl));
    this.gammaLines = [];
    this.updateTitle(g && g.ok ? g.local_sign : null);
    drawGammaPanel(g);
    if (!g || !g.ok) return;
    for (const f of (g.flips || [])) {
      this.addGammaLine(f, "#e6a9ff", LightweightCharts.LineStyle.Solid, "flip");
    }
    for (const [k] of (g.call_walls || [])) {
      this.addGammaLine(k, "#ffa500", LightweightCharts.LineStyle.Dashed, "call wall");
    }
    for (const [k] of (g.put_walls || [])) {
      this.addGammaLine(k, "#1e90ff", LightweightCharts.LineStyle.Dashed, "put wall");
    }
    if (g.pocket && g.pocket[0] != null && g.pocket[1] != null) {
      this.addGammaLine(g.pocket[0], "#4f9cf9",
                        LightweightCharts.LineStyle.Dotted, "pocket");
      this.addGammaLine(g.pocket[1], "#4f9cf9",
                        LightweightCharts.LineStyle.Dotted, "pocket");
    }
  }

  /* ── strategy roster: name + show/hide toggle per engine strategy ─────── */
  setRoster(roster) {
    this.roster = roster || [];
    for (const e of this.roster) {
      if (!(e.label in this.strategyOn)) this.strategyOn[e.label] = !!e.enabled;
    }
    this.renderRoster();
    this.renderStrategyMarks();
  }

  renderRoster() {
    const host = $("roster");
    if (!host) return;
    host.innerHTML = "";
    for (const e of this.roster) {
      const on = this.strategyOn[e.label];
      const row = document.createElement("div");
      row.className = "rosterRow" + (on ? "" : " off");
      row.innerHTML =
        `<span class="sw ${on ? "on" : ""}" data-sw="1"></span>` +
        `<span class="name">${escapeHtml(e.label)}</span>` +
        (e.qty ? `<span class="posTag">${e.qty > 0 ? "+" : ""}${e.qty}</span>` : "") +
        `<span class="dim">${overlaySummary(e)}</span>`;
      row.querySelector(".sw").addEventListener("click", () => {
        this.strategyOn[e.label] = !this.strategyOn[e.label];
        this.renderRoster();
        this.renderStrategyMarks();
      });
      host.appendChild(row);
    }
  }

  renderStrategyMarks() {
    // remove all strategy price lines, redraw those whose toggle is on
    for (const label in this.strategyLines) {
      (this.strategyLines[label] || []).forEach(pl =>
        this.candleSeries.removePriceLine(pl));
    }
    this.strategyLines = {};
    for (const e of this.roster) {
      if (this.strategyOn[e.label] === false) continue;
      const marks = [["entry", e.entry, "#e2a03f", LightweightCharts.LineStyle.Solid,
                      "entry"],
                    ["target", e.target, "#26a69a", LightweightCharts.LineStyle.Dashed,
                     "target"],
                    ["stop", e.stop, "#ef5350", LightweightCharts.LineStyle.Dashed,
                     "stop"]];
      const pls = [];
      for (const [name, val, color, style, label] of marks) {
        if (val == null) continue;
        pls.push(this.candleSeries.createPriceLine({ price: val, color,
          lineWidth: 1, lineStyle: style, axisLabelVisible: true,
          title: `${e.label} ${label}` }));
      }
      this.strategyLines[e.label] = pls;
    }
  }

  setPositions(list) {
    this.positions = list || [];
    this.drawPositions();
  }
  applyPosition(p) {
    const idx = this.positions.findIndex(x => x.sleeve === (p && p.sleeve));
    if (p) { if (idx >= 0) this.positions[idx] = p; else this.positions.push(p); }
    else if (idx >= 0) this.positions.splice(idx, 1);
    this.drawPositions();
  }
  drawPositions() {
    this.positionLines.forEach(pl => this.candleSeries.removePriceLine(pl));
    this.positionLines = [];
    for (const p of this.positions) {
      for (const [name, val, color, style] of [["entry", p.entry, "#e2a03f",
            LightweightCharts.LineStyle.Solid],
          ["target", p.target, "#26a69a", LightweightCharts.LineStyle.Dashed],
          ["stop", p.stop, "#ef5350", LightweightCharts.LineStyle.Dashed]]) {
        if (val == null) continue;
        this.positionLines.push(this.candleSeries.createPriceLine({
          price: val, color, lineWidth: 1, lineStyle: style,
          axisLabelVisible: true, title: `${p.sleeve} ${name}` }));
      }
    }
  }

  setZones(list) {
    this.zones = list || [];
    this.zoneLines.forEach(pl => this.candleSeries.removePriceLine(pl));
    this.zoneLines = [];
    for (const z of this.zones) {
      for (const [edge, nm] of [[z.top, "top"], [z.bot, "bot"]]) {
        if (edge == null) continue;
        this.zoneLines.push(this.candleZoneLine(z, edge, nm));
      }
    }
  }
  candleZoneLine(z, price, nm) {
    const demand = (z.direction || 0) > 0;
    return this.candleSeries.createPriceLine({ price, lineWidth: 1,
      color: demand ? "#26a69a" : "#ef5350",
      lineStyle: LightweightCharts.LineStyle.Dotted,
      axisLabelVisible: true, title: `${z.tf} ${nm}` });
  }

  /* ── WebSocket: this window subscribes to its own symbol/tf ──────────── */
  connect() {
    if (this.ws) { try { this.ws.close(); } catch (e) {} this.ws = null; }
    const url = `ws://${location.host}/ws?symbol=${encodeURIComponent(this.symbol)}&tf=${encodeURIComponent(this.tf)}`;
    this.ws = new WebSocket(url);
    const me = this;
    this.ws.onopen = () => setStatus(window.status, "ok", "connected");
    this.ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      me.onMessage(msg);
    };
    this.ws.onclose = () => { setStatus(window.status, "err", "disconnected — retrying");
      this.ws = null; setTimeout(() => me.connect(), 1500); };
    this.ws.onerror = () => setStatus(window.status, "err", "ws error");
  }

  onMessage(msg) {
    switch (msg.type) {
      case "hello": {
        LANES = msg.lanes; TIMEFRAMES = msg.timeframes;
        populateSelects(msg.symbol, msg.tf);
        document.title = `es-engine · ${this.symbol} ${this.tf}`;
        break;
      }
      case "snapshot": {
        const p = msg.payload || {};
        this.symbol = msg.symbol || this.symbol;
        this.tf = msg.tf || this.tf;
        this.setBars(p.bars || []);
        for (const ind of p.indicators || []) {
          this.storeIndicator(ind);
          this.upsertIndicator(ind);
        }
        this.renderGamma(p.gamma);
        this.setRoster(p.strategies_roster || []);
        this.setZones(p.zones || []);
        this.setPositions(p.positions || []);
        this.redrawBands();
        for (const rec of msg.log || []) appendLogLine(rec, false);
        for (const rec of msg.errors || []) appendLogLine(rec, true);
        setStatus(window.status, "ok", "live");
        break;
      }
      case "delta": this.applyDelta(msg); break;
      case "pong": break;
    }
  }

  storeIndicator(ind) {
    if (!ind || !ind.id) return;
    this.indicatorData[ind.id] = { label: ind.label, producer: ind.producer,
      points: ind.points || [] };
  }

  applyDelta(msg) {
    const pl = msg.payload || {};
    switch (msg.kind) {
      case "bar": this.applyBar(pl.bar); break;
      case "indicator":
        this.storeIndicator(pl);
        this.upsertIndicator(pl);
        break;
      case "gamma": this.renderGamma(pl.gamma); break;
      case "roster":
        if (pl.roster) this.setRoster(pl.roster);
        break;
      case "zone": this.applyZoneDelta(pl); break;
      case "position": this.applyPosition(pl.position); break;
      case "fill": this.addFillMarker(pl); break;
      case "log": appendLogLine(pl, false); break;
      case "error": appendLogLine(pl, true); break;
      case "session":
      case "lane": break;
    }
  }

  applyZoneDelta(pl) {
    if (pl.added || pl.updated || pl.removed) {
      const m = {};
      for (const z of this.zones) m[z.id] = z;
      for (const rm of pl.removed || []) delete m[rm];
      for (const z of pl.added || []) m[z.id] = z;
      for (const z of pl.updated || []) m[z.id] = z;
      this.zones = Object.values(m);
      this.zoneLines.forEach(pl => this.candleSeries.removePriceLine(pl));
      this.zoneLines = [];
      for (const z of this.zones) {
        for (const [edge, nm] of [[z.top, "top"], [z.bot, "bot"]]) {
          if (edge == null) continue;
          this.zoneLines.push(this.candleZoneLine(z, edge, nm));
        }
      }
    }
  }

  addFillMarker(f) {
    if (!f) return;
    const side = (f.size || 0) > 0;
    const mk = this.candleSeries; // markers live on candle series
    this._marks = this._marks || [];
    this._marks.push({ time: sec(f.ts || Date.now() * 1e6),
      position: side ? "belowBar" : "aboveBar",
      color: side ? "#26a69a" : "#ef5350",
      shape: side ? "arrowUp" : "arrowDown",
      text: (side ? "B " : "S ") + (f.tag || "") + " @" + (f.price ?? "") });
    mk.setMarkers(this._marks.slice(-200));
  }
}

/* ═══════════════ page-level helpers (drawn across all windows) ════════ */
function setStatus(el, cls, text) { if (el) { el.textContent = text; el.className = "status " + cls; } }

function populateSelects(symbol, tf) {
  const ls = $("laneSel"), ts = $("tfSel");
  ls.innerHTML = ""; ts.innerHTML = "";
  for (const l of LANES) {
    const o = document.createElement("option"); o.value = l; o.textContent = l;
    if (l === symbol) o.selected = true; ls.appendChild(o);
  }
  for (const t of TIMEFRAMES) {
    const o = document.createElement("option"); o.value = t; o.textContent = t;
    if (t === tf) o.selected = true; ts.appendChild(o);
  }
}

function overlaySummary(e) {
  const parts = [];
  if (e.entry != null) parts.push("entry " + fmtPx(e.entry));
  if (e.target != null) parts.push("T " + fmtPx(e.target));
  if (e.stop != null) parts.push("S " + fmtPx(e.stop));
  return parts.join(" · ") || (e.enabled ? "overlay on" : "flat");
}

function drawGammaPanel(g) {
  const host = $("gammaBody");
  if (!host) return; host.innerHTML = "";
  if (!g || !g.ok) { host.textContent = "No dealer-gamma data."; return; }
  const regime = g.local_sign > 0 ? "LONG" : g.local_sign < 0 ? "SHORT" : "FLAT";
  const re = document.createElement("div");
  re.className = "gammaRegime " + (g.local_sign > 0 ? "long" : g.local_sign < 0 ? "short" : "");
  re.innerHTML = `<span>Regime <strong>${regime} γ</strong></span>` +
    `<span>@ ${fmtPx(g.price)}</span>` +
    (g.dist_to_edge != null ? `<span>(${g.dist_to_edge > 0 ? "+" : ""}${fmtPx(g.dist_to_edge)})</span>` : "");
  host.appendChild(re);

  const addRow = (swatch, kind, px, gex) => {
    const r = document.createElement("div");
    r.className = "gammaRow";
    r.innerHTML = `<span class="swatch" style="background:${swatch}"></span>` +
      `<span class="kind">${kind}</span><span class="px">${px}</span>` +
      (gex ? `<span class="gex">${gex}</span>` : "");
    host.appendChild(r);
  };
  for (const f of (g.flips || [])) addRow("var(--flip)", "flip", fmtPx(f));
  for (const [k, n] of (g.call_walls || []))
    addRow("var(--call)", "call wall", fmtPx(k), n != null ? fmtPx(n) : "");
  for (const [k, n] of (g.put_walls || []))
    addRow("var(--put)", "put wall", fmtPx(k), n != null ? fmtPx(n) : "");
  if (g.pocket && (g.pocket[0] != null || g.pocket[1] != null)) {
    const lo = g.pocket[0] != null ? fmtPx(g.pocket[0]) : "—";
    const hi = g.pocket[1] != null ? fmtPx(g.pocket[1]) : "—";
    addRow("var(--accent)", "pocket", `${lo} → ${hi}`);
  }
}

/* ── log ─────────────────────────────────────────────────────────────── */
const LOG_MAX = 300;
function appendLogLine(rec, isError) {
  const host = $("log");
  if (!host || !rec) return;
  const line = document.createElement("div");
  const d = new Date(rec.ts ? rec.ts / 1e6 : Date.now());
  line.className = "logline " + (rec.level || "").toLowerCase();
  line.innerHTML = `<span class="t">${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}</span>` +
    `<span class="msg">${escapeHtml(rec.level || "")} ${escapeHtml(rec.message || "")}</span>`;
  host.appendChild(line);
  while (host.children.length > LOG_MAX) host.removeChild(host.firstChild);
  host.scrollTop = host.scrollHeight;
  if (isError || rec.level === "ERROR" || rec.level === "CRITICAL") renderErrorBadge(1);
}
function renderErrorBadge(n) {
  const badge = $("errorBadge");
  if (!badge) return;
  if (n) {
    badge.classList.remove("hidden");
    badge.textContent = (parseInt(badge.textContent || "0", 10) + (typeof n === "number" ? n : 0));
    setStatus(window.status, "err", "warnings");
  } else {
    badge.classList.add("hidden"); badge.textContent = "0";
  }
}

/* ── window management: add / pop-out / grid ─────────────────────────── */
function popOutUrl(symbol, tf) {
  window.open(`${location.origin}/?symbol=${encodeURIComponent(symbol)}&tf=${encodeURIComponent(tf)}`, "_blank");
}
function addWindow(symbol, tf) {
  const id = nextWinId();
  const w = new ChartWindow(id, symbol || (LANES[0] || "ES"), tf || (TIMEFRAMES[0] || "5m"));
  windows.set(id, w);
  updateGrid();
  return w;
}
function updateGrid() {
  const n = windows.size;
  document.body.classList.toggle("grid2up", n > 1);
  // first window becomes the main one; others show a close button
  let first = true;
  windows.forEach(w => { w.closeBtn.classList.toggle("hidden", first); first = false; setTimeout(() => w.reflow(), 0); });
}
function toggleGrid() {
  // if only one window, add a second inline; else collapse back to one
  if (windows.size < 2) addWindow(nextLane(), TIMEFRAMES[0]);
  // when >1 the grid 2-up is automatic; collapse when user wants single
  if (windows.size > 1 && document.body.classList.contains("grid2up")) {
    // keep only the first
    const it = windows.keys(); it.next(); const extra = it.next().value;
    if (extra) windows.get(extra).destroy();
  }
  updateGrid();
}
function nextLane() { const s = new Set(windows.values()); for (const l of LANES) if (!s.has(l)) return l; return LANES[0]; }

/* ── topbar wiring ───────────────────────────────────────────────────── */
function mainWindow() { return windows.values().next().value; }

$("laneSel").addEventListener("change", (ev) => {
  const w = mainWindow(); if (!w) return;
  w.symbol = ev.target.value; w.updateTitle(w.gamma ? w.gamma.local_sign : null);
  w.candles = []; w.indicatorData = {}; w.connect();
});
$("tfSel").addEventListener("change", (ev) => {
  const w = mainWindow(); if (!w) return;
  w.tf = ev.target.value; w.candles = []; w.indicatorData = {}; w.connect();
});
$("popBtn").addEventListener("click", () => {
  const w = mainWindow(); if (w) popOutUrl(w.symbol, w.tf);
});
$("plusBtn").addEventListener("click", () => {
  popOutUrl(LANES[0], TIMEFRAMES[0]);
});
$("gridBtn").addEventListener("click", toggleGrid);

function drawer(id) { return $(id); }
$("stratBtn").addEventListener("click", () => {
  drawer("stratDrawer").classList.toggle("hidden");
  (mainWindow() || {}).renderRoster && mainWindow().renderRoster();
});
$("gammaBtn").addEventListener("click", () => {
  drawer("gammaDrawer").classList.toggle("hidden");
});
$("errorBadge").addEventListener("click", () => drawer("logDrawer").classList.toggle("hidden"));
$("status").addEventListener("click", () => drawer("logDrawer").classList.toggle("hidden"));

/* ── boot ────────────────────────────────────────────────────────────── */
setStatus($("status"), "", "connecting…");
addWindow(qs.get("symbol") || "ES", qs.get("tf") || "5m");
