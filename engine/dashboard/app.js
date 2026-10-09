/* es-engine dashboard client: renders the engine's published state (bars,
   indicators, zones, positions) — never re-derives an indicator or zone in
   JS; it only draws what the engine publishes. One WS per window; symbol +
   timeframe are window-scoped (from URL, changeable in the topbar). */

"use strict";

const $ = (id) => document.getElementById(id);

const qs = new URLSearchParams(location.search);

// per-window config (defaults)
let SYMBOL = qs.get("symbol") || "ES";
let TF = qs.get("tf") || "5m";
let LANES = ["ES"];
let TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];

/* ── chart setup ─────────────────────────────────────────────────────── */
const chartEl = $("chart");
function chartSize() {
  const r = chartEl.getBoundingClientRect();
  return [Math.max(300, Math.floor(r.width)), Math.max(180, Math.floor(r.height))];
}
const [initW, initH] = chartSize();
const chart = LightweightCharts.createChart(chartEl, {
  width: initW,
  height: initH,
  layout: { background: { type: "solid", color: "#0d1117" },
            textColor: "#dbe2ea" },
  grid: { vertLines: { color: "#1a2230" }, horzLines: { color: "#1a2230" } },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  rightPriceScale: { borderColor: "#2b3340" },
  timeScale: { borderColor: "#2b3340", timeVisible: true,
               secondsVisible: false },
});

// reflow the chart if its container resizes (and on initial layout settle)
let resizeRaf = 0;
function reflowChart() {
  cancelAnimationFrame(resizeRaf);
  resizeRaf = requestAnimationFrame(() => {
    const [w, h] = chartSize();
    chart.resize(w, h);
    sizeCanvas();
    draw();
  });
}
const ro = new ResizeObserver(() => reflowChart());
ro.observe(chartEl);

const candleSeries = chart.addCandlestickSeries({
  upColor: "#26a69a", downColor: "#ef5350",
  borderUpColor: "#26a69a", borderDownColor: "#ef5350",
  wickUpColor: "#8fd0c9", wickDownColor: "#f0a0a0",
});
let candles = [];

// indicator line series per indicator id
const indSeries = {};
// price lines for static levels (pivots/gamma) — added to the candle series
const zonePriceLines = [];

/* ── zone overlay (canvas) for geometrically-correct bands ─────────── */
const cv = $("zoneCanvas");
const ctx = cv.getContext("2d");
let zones = [];

function sizeCanvas() {
  const r = chartEl.getBoundingClientRect();
  cv.style.width = cv.style.height = "";
  cv.width = r.width * devicePixelRatio;
  cv.height = r.height * devicePixelRatio;
  cv.style.width = r.width + "px";
  cv.style.height = r.height + "px";
  ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
}
sizeCanvas();
// (chart resize handled by ResizeObserver on chartEl)

function draw() {
  const r = chartEl.getBoundingClientRect();
  ctx.clearRect(0, 0, r.width, r.height);
  const ts = chart.timeScale();
  const lo = ts.timeToCoordinate(ts.getVisibleRange().from);
  const hi = ts.timeToCoordinate(ts.getVisibleRange().to);
  if (lo == null || hi == null) return;
  for (const z of zones) {
    const yTop = candleSeries.priceToCoordinate(z.top);
    const yBot = candleSeries.priceToCoordinate(z.bot);
    if (yTop == null || yBot == null) continue;
    const demand = (z.direction || 0) > 0;
    ctx.fillStyle = demand ? "rgba(38,166,154,0.10)" : "rgba(239,83,80,0.10)";
    ctx.strokeStyle = demand ? "rgba(38,166,154,0.6)" : "rgba(239,83,80,0.6)";
    ctx.lineWidth = 1;
    const topY = Math.min(yTop, yBot), botY = Math.max(yTop, yBot);
    ctx.fillRect(lo, topY, hi - lo, botY - topY);
    ctx.strokeRect(lo, topY, hi - lo, botY - topY);
    // label the zone near its centroid
    ctx.fillStyle = demand ? "#8fd0c9" : "#f0a0a0";
    ctx.font = "10px -apple-system, Segoe UI, sans-serif";
    const midY = (topY + botY) / 2;
    ctx.textBaseline = "middle";
    const txt = z.tf + (z.virgin ? " virgin" : "") + " " + (z.touches ? z.touches + "t" : "");
    ctx.fillText(txt, Math.min(lo + 6, hi - 40), midY);
  }
}
chart.timeScale().subscribeVisibleLogicalRangeChange(() => draw());
chart.timeScale().subscribeVisibleTimeRangeChange(() => draw());

/* ── light-charts time (epoch sec) ← engine bar ts (epoch ns) ─────────── */
const sec = (ns) => Math.floor(ns / 1e9);

/* ── indicator series management ───────────────────────────────────────*/
function upsertIndicator(ind, clearFirst) {
  if (!ind || !ind.id || !ind.points || !ind.points.length) return;
  if (clearFirst && zones && false) { /* noop */ }
  let ser = indSeries[ind.id];
  if (!ser) {
    const def = defaultIndStyle(ind);
    ser = chart.addLineSeries({ ...def.style, priceLineVisible: false,
                                lastValueVisible: false, lineWidth: 1 });
    indSeries[ind.id] = ser;
  }
  // build the line: for pivots/gamma (static intraday grid) we draw each level
  // as a horizontal line across the visible range by emitting two points; for
  // time series (vwap) we use the real {ts,v} samples only.
  const isStatic = ind.producer.includes("pivot") ||
                   ind.producer.includes("gamma");
  if (isStatic) {
    if (!ser._static) { ser.setData([]); ser._static = true; }
    drawStaticLevels(ind);
  } else {
    ser.setData(ind.points.map(p => ({ time: sec(p.ts), value: p.v }))
                .sort((a, b) => a.time - b.time));
  }
}

function defaultIndStyle(ind) {
  const p = ind.producer || "";
  let color = "#4f9cf9";
  if (p.includes("vwap")) color = "#e2a03f";
  else if (p.includes("pivot")) color = "#a78bfa";
  else if (p.includes("gamma")) color = "#4f9cf9";
  return { style: { color, title: ind.label || ind.id } };
}

function drawStaticLevels(ind) {
  // remove previous static price lines from this indicator on the candle series
  (ind._lines || []).forEach(pl => candleSeries.removePriceLine(pl));
  ind._lines = [];
  const seen = new Set();
  // each distinct price level -> a dashed horizontal line across all time
  for (const p of ind.points) {
    const key = p.v.toFixed(4);
    if (seen.has(key)) continue;
    seen.add(key);
    const pl = candleSeries.createPriceLine({
      price: p.v,
      color: defaultIndStyle(ind).style.color,
      lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true,
      title: ind.label || ind.id,
    });
    ind._lines.push(pl);
  }
}

/* ── positions: entry/target/stop markers + side rail ──────────────────*/
let positions = [];
function renderPositions() {
  const host = $("positions");
  host.innerHTML = positions.map(p => {
    const cls = p.qty > 0 ? "long" : "short";
    return `<div class="pos ${cls}">
      <div class="tag">${escapeHtml(p.sleeve)}</div>
      <div class="row"><span class="lbl">pos</span>
        <span class="px">${p.qty > 0 ? "+" : ""}${p.qty}</span></div>
      <div class="row"><span class="lbl">entry</span>
        <span class="px">${fmtPx(p.entry)}</span></div>
      <div class="row"><span class="lbl">target</span>
        <span class="px">${fmtPx(p.target)}</span></div>
      <div class="row"><span class="lbl">stop</span>
        <span class="px">${fmtPx(p.stop)}</span></div>
    </div>`;
  }).join("");
  drawPositionMarkers();
}

// price lines for the position rails on the candle series
let positionPriceLines = [];
function drawPositionMarkers() {
  positionPriceLines.forEach(pl => candleSeries.removePriceLine(pl));
  positionPriceLines = [];
  for (const p of positions) {
    for (const [name, px] of [["entry", p.entry], ["target", p.target],
                              ["stop", p.stop]]) {
      if (px == null) continue;
      const color = name === "target" ? "var(--green)" : "var(--red)";
      const colorVal = name === "target" ? "#26a69a"
                       : name === "stop" ? "#ef5350" : "#e2a03f";
      const pl = candleSeries.createPriceLine({
        price: px, color: colorVal, lineWidth: 1,
        lineStyle: name === "entry"
          ? LightweightCharts.LineStyle.Solid
          : LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: `${p.sleeve} ${name}`,
      });
      positionPriceLines.push(pl);
    }
  }
}

/* ── fills → markers ───────────────────────────────────────────────────*/
let fillMarkers = [];
function addFillMarker(f) {
  const side = (f.size || 0) > 0;
  fillMarkers.push({
    time: sec(f.ts || Date.now() * 1e6),
    position: side ? "belowBar" : "aboveBar",
    color: side ? "#26a69a" : "#ef5350",
    shape: side ? "arrowUp" : "arrowDown",
    text: (side ? "B " : "S ") + (f.tag || "") + " @ " + (f.price ?? ""),
  });
  applyMarkers();
}
function applyMarkers() {
  candleSeries.setMarkers(fillMarkers.slice(-200));
}

/* ── zones management ──────────────────────────────────────────────────*/
let zoneMap = {};   // id -> ZoneDict
function setZones(list) {
  zoneMap = {};
  for (const z of list) zoneMap[z.id] = z;
  zones = Object.values(zoneMap).sort((a, b) => a.top - b.top);
  draw();
  renderZoneList();
}
function applyZoneDelta(pl) {
  for (const rm of pl.removed || []) delete zoneMap[rm];
  for (const z of pl.added || []) zoneMap[z.id] = z;
  for (const z of pl.updated || []) zoneMap[z.id] = z;
  zones = Object.values(zoneMap).sort((a, b) => a.top - b.top);
  draw();
  renderZoneList();
}
function renderZoneList() {
  // compact list in the indicators panel
  const host = $("indicators");
  const zoneLines = zones.map(z =>
    `<div class="ind"><span class="id">${z.tf}</span> zone ` +
    `${z.direction > 0 ? "🠕" : "🠗"} ${z.top}→${z.bot}` +
    `${z.virgin ? " ·virgin" : ""}` +
    `<span class="prod"> ${z.touches}t</span></div>`).join("");
  const indLines = Object.values(indSeries).map(s => {
    const label = s._label || s.options().title || s._id;
    return `<div class="ind"><span class="id">${escapeHtml(label)}</span>` +
           `<span class="prod"> ${escapeHtml(s._producer || "")}</span></div>`;
  }).join("");
  host.innerHTML = zoneLines + indLines;
}

/* ── logs ──────────────────────────────────────────────────────────────*/
function appendLog(rec, isError) {
  const host = $("log");
  const line = document.createElement("div");
  const d = new Date(rec.ts ? rec.ts / 1e6 : Date.now());
  line.className = "logline " + (rec.level || "").toLowerCase();
  line.innerHTML = `<span class="t">${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}</span>` +
    `<span class="lvl">${escapeHtml(rec.level || "")}</span>` +
    `<span class="msg">${escapeHtml(rec.message || "")}</span>`;
  host.appendChild(line);
  while (host.children.length > 300) host.removeChild(host.firstChild);
  host.scrollTop = host.scrollHeight;
  if (isError || (rec.level === "ERROR" || rec.level === "WARNING" ||
                  rec.level === "CRITICAL")) {
    const badge = $("errorBadge");
    badge.classList.remove("hidden");
    let n = parseInt(badge.textContent || "0", 10);
    badge.textContent = (n + 1);
    $("status").className = "err";
  }
}

/* ── helpers ───────────────────────────────────────────────────────────*/
function fmtPx(x) { return x == null ? "—" : Number(x).toFixed(2); }
function pad(n) { return String(n).padStart(2, "0"); }
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ── WS client ─────────────────────────────────────────────────────────*/
function connect() {
  const url = `ws://${location.host}/ws?symbol=${encodeURIComponent(SYMBOL)}&tf=${encodeURIComponent(TF)}`;
  const ws = new WebSocket(url);
  let hello = null;

  ws.onopen = () => { $("status").textContent = "connected"; $("status").className = "ok"; };
  ws.onmessage = (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
    switch (msg.type) {
      case "hello":
        hello = msg;
        LANES = msg.lanes; TIMEFRAMES = msg.timeframes;
        SYMBOL = msg.symbol; TF = msg.tf;
        populateControls();
        document.title = `es-engine · ${SYMBOL} ${TF}`;
        break;
      case "snapshot": {
        const p = msg.payload;
        candles = p.bars || [];
        candleSeries.setData(candles.map(b => ({
          time: sec(b.ts), open: b.o, high: b.h, low: b.l, close: b.c,
        })));
        applyMarkers();
        for (const ind of p.indicators || []) {
          indSeries[ind.id] = null;          // rebuild on snapshot
          upsertIndicator(ind);
          const s = indSeries[ind.id];
          if (s) { s._label = ind.label; s._producer = ind.producer; s._id = ind.id; }
        }
        setZones(p.zones || []);
        positions = p.positions || [];
        renderPositions();
        if (hello) { SYMBOL = msg.symbol; TF = msg.tf; refreshHeader(); }
        for (const rec of msg.log || []) appendLog(rec, false);
        for (const rec of msg.errors || []) appendLog(rec, true);
        break;
      }
      case "delta": applyDelta(msg); break;
      case "pong": break;
    }
  };
  ws.onclose = () => {
    $("status").textContent = "disconnected — retrying";
    $("status").className = "err";
    setTimeout(connect, 1500);
  };
  ws.onerror = () => { $("status").textContent = "ws error"; $("status").className = "err"; };
  return ws;
}

function applyDelta(msg) {
  const kind = msg.kind, pl = msg.payload || {};
  switch (kind) {
    case "bar": {
      const b = pl.bar;
      if (!b) return;
      const datum = { time: sec(b.ts), open: b.o, high: b.h, low: b.l, close: b.c };
      const last = candles.length ? candles[candles.length - 1] : null;
      if (last && last.ts === b.ts) {
        candles[candles.length - 1] = b;
        candleSeries.update(datum);
      } else {
        candles.push(b);
        candleSeries.update(datum);
      }
      break;
    }
    case "indicator": {
      upsertIndicator(pl);
      const s = indSeries[pl.id];
      if (s) { s._label = pl.label; s._producer = pl.producer; s._id = pl.id; }
      renderZoneList();
      break;
    }
    case "zone": {
      if (pl.added || pl.updated || pl.removed) applyZoneDelta(pl);
      else if (pl.id) applyZoneDelta({ added: [pl], updated: [], removed: [] });
      break;
    }
    case "position": {
      const p = pl.position;
      const idx = positions.findIndex(x => x.sleeve === (p && p.sleeve));
      if (p) {
        if (idx >= 0) positions[idx] = p; else positions.push(p);
      } else if (idx >= 0) {
        positions.splice(idx, 1);
      }
      renderPositions();
      break;
    }
    case "fill": addFillMarker(pl); break;
    case "log": appendLog(pl, false); break;
    case "error": appendLog(pl, true); break;
    case "session":
    case "lane": break;
  }
}

/* ── topbar controls ───────────────────────────────────────────────────*/
function populateControls() {
  const ls = $("laneSel"); ls.innerHTML = "";
  for (const l of LANES) {
    const o = document.createElement("option");
    o.value = l; o.textContent = l;
    if (l === SYMBOL) o.selected = true;
    ls.appendChild(o);
  }
  const ts = $("tfSel"); ts.innerHTML = "";
  for (const t of TIMEFRAMES) {
    const o = document.createElement("option");
    o.value = t; o.textContent = t;
    if (t === TF) o.selected = true;
    ts.appendChild(o);
  }
  refreshHeader();
}
function refreshHeader() {
  $("chartLegend").textContent = `${SYMBOL} · ${TF} · live`;
}
function reconfigure() {
  SYMBOL = $("laneSel").value;
  TF = $("tfSel").value;
  document.title = `es-engine · ${SYMBOL} ${TF}`;
  refreshHeader();
  connect();   // new WS scoped to this window's symbol+tf
}
$("laneSel").addEventListener("change", reconfigure);
$("tfSel").addEventListener("change", reconfigure);
$("linkBtn").addEventListener("click", () => {
  const u = new URL(location.href);
  u.searchParams.set("symbol", SYMBOL);
  u.searchParams.set("tf", TF);
  navigator.clipboard && navigator.clipboard.writeText(u.toString());
});

// start
populateControls();
connect();
