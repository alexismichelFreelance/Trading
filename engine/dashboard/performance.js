"use strict";
const $ = (id) => document.getElementById(id);

let chartAgg = null;
let chartDrill = null;
let chartBar = null;
let perfData = null;
let currentPeriod = "day";
let barPeriod = "day";
let selectedStrats = new Set(); // empty = all

// ---------- helpers ----------
function fmtPnl(v) { return (v >= 0 ? "+" : "") + Number(v).toFixed(2); }
function pnlClass(v) { return v >= 0 ? "pos" : "neg"; }
function escapeHtml(s) { return String(s == null ? "" : s).replace(/[&<>\"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function parseTs(s) { const d = new Date(String(s).replace(" ", "T")); return isNaN(d.getTime()) ? null : d; }
function fmtDate(d) { return d.toISOString().slice(0, 10); }

const sortState = {};

function makeSortable(tableId, tbodyId, data, columns) {
  const table = $(tableId);
  const thead = table.querySelector("thead");
  if (!table._sortInit) {
    table._sortInit = true;
    thead.querySelectorAll("th[data-sort]").forEach(th => {
      th.addEventListener("click", () => {
        const key = th.dataset.sort;
        if (!sortState[tableId]) sortState[tableId] = { key, dir: 1 };
        else if (sortState[tableId].key === key) sortState[tableId].dir *= -1;
        else sortState[tableId] = { key, dir: 1 };
        renderSortedTable(tableId, tbodyId, data, columns);
        updateSortArrows(tableId);
      });
    });
  }
  if (!sortState[tableId]) sortState[tableId] = { key: columns[0].key || columns[0], dir: 1 };
  renderSortedTable(tableId, tbodyId, data, columns);
  updateSortArrows(tableId);
}

function renderSortedTable(tableId, tbodyId, data, columns) {
  const st = sortState[tableId];
  const sorted = [...data].sort((a, b) => {
    let va = a[st.key], vb = b[st.key];
    if (typeof va === "string") { va = va.toLowerCase(); vb = (vb || "").toLowerCase(); }
    if (va < vb) return -1 * st.dir;
    if (va > vb) return 1 * st.dir;
    return 0;
  });
  const tbody = $(tbodyId);
  tbody.innerHTML = "";
  for (const row of sorted) {
    const tr = document.createElement("tr");
    tr.innerHTML = columns.map(c => {
      if (c.render) return `<td class="${c.cls ? c.cls(row) : ''}">${c.render(row)}</td>`;
      const v = row[c.key];
      const cls = c.cls ? c.cls(row) : '';
      return `<td class="${cls}">${v ?? ''}</td>`;
    }).join("");
    tr._rowData = row;
    tbody.appendChild(tr);
  }
}

function updateSortArrows(tableId) {
  const table = $(tableId);
  const st = sortState[tableId];
  table.querySelectorAll("th[data-sort]").forEach(th => {
    const arrow = th.querySelector(".sortArrow");
    if (th.dataset.sort === st.key) arrow.textContent = st.dir > 0 ? "▲" : "▼";
    else arrow.textContent = "";
  });
}

// ---------- P&L by period ----------
function aggregateByPeriod(trades, period) {
  const buckets = {};
  for (const t of trades) {
    const d = parseTs(t.exit_ts);
    if (!d) continue;
    let key;
    if (period === "day") key = fmtDate(d);
    else if (period === "week") {
      const monday = new Date(d);
      monday.setDate(d.getDate() - ((d.getDay() + 6) % 7));
      key = fmtDate(monday);
    } else if (period === "month") {
      key = d.toISOString().slice(0, 7);
    } else if (period === "year") {
      key = d.toISOString().slice(0, 4);
    }
    if (!buckets[key]) buckets[key] = { period: key, trades: 0, wins: 0, pnl: 0 };
    buckets[key].trades++;
    if (t.pnl > 0) buckets[key].wins++;
    buckets[key].pnl += t.pnl;
  }
  const rows = Object.values(buckets).sort((a, b) => a.period < b.period ? -1 : 1);
  let cum = 0;
  for (const r of rows) { cum += r.pnl; r.cumulative = cum; r.pnl = Math.round(r.pnl * 100) / 100; r.cumulative = Math.round(r.cumulative * 100) / 100; }
  return rows;
}

function periodToTs(period, periodType) {
  // convert period string to unix timestamp (seconds)
  if (periodType === "day" || periodType === "week") {
    return Math.floor(new Date(period + "T00:00:00Z").getTime() / 1000);
  } else if (periodType === "month") {
    return Math.floor(new Date(period + "-01T00:00:00Z").getTime() / 1000);
  } else if (periodType === "year") {
    return Math.floor(new Date(period + "-01-01T00:00:00Z").getTime() / 1000);
  }
  return 0;
}

// ---------- load ----------
async function loadPerformance() {
  const sym = $("symSel").value;
  const from = $("fromDate").value;
  const to = $("toDate").value;
  $("loading").style.display = "block";
  $("content").style.display = "none";
  let url = `/api/performance?symbol=${encodeURIComponent(sym)}`;
  if (from) url += `&from=${from}`;
  if (to) url += `&to=${to}`;
  try {
    const resp = await fetch(url);
    const data = await resp.json();
    if (data.error) { $("loading").textContent = "Error: " + data.error; return; }
    perfData = data;
    populateStratSelector(data);
    populateStratFilter(data);
    renderAllView(data);
    renderBarChart();
    $("loading").style.display = "none";
    $("content").style.display = "block";
    updateViewVisibility();
  } catch (e) {
    $("loading").textContent = "Failed to load: " + e.message;
  }
}

function populateStratSelector(data) {
  const sel = $("stratSel");
  sel.innerHTML = "";
  for (const s of (data.strategies || [])) {
    const o = document.createElement("option"); o.value = s.sleeve; o.textContent = s.sleeve;
    sel.appendChild(o);
  }
}

function populateStratFilter(data) {
  const host = $("stratFilter");
  host.innerHTML = "";
  // "All" checkbox
  const allLbl = document.createElement("label");
  allLbl.innerHTML = `<input type="checkbox" value="__ALL__" checked> All strategies`;
  host.appendChild(allLbl);
  for (const s of (data.strategies || [])) {
    const lbl = document.createElement("label");
    lbl.innerHTML = `<input type="checkbox" value="${escapeHtml(s.sleeve)}" checked> ${escapeHtml(s.sleeve)}`;
    host.appendChild(lbl);
  }
  host.querySelectorAll("input").forEach(cb => {
    cb.addEventListener("change", () => {
      if (cb.value === "__ALL__") {
        const checked = cb.checked;
        host.querySelectorAll("input").forEach(c => c.checked = checked);
      }
      updateSelectedStrats();
      renderBarChart();
    });
  });
  updateSelectedStrats();
}

function updateSelectedStrats() {
  const host = $("stratFilter");
  const allCb = host.querySelector('input[value="__ALL__"]');
  const cbs = [...host.querySelectorAll('input')].filter(c => c.value !== "__ALL__");
  if (allCb && allCb.checked) {
    selectedStrats = new Set(cbs.map(c => c.value));
  } else {
    selectedStrats = new Set(cbs.filter(c => c.checked).map(c => c.value));
  }
}

function updateViewVisibility() {
  const view = $("viewSel").value;
  $("stratLabel").style.display = view === "single" ? "" : "none";
  $("allView").style.display = view === "all" ? "block" : "none";
  $("singleView").style.display = view === "single" ? "block" : "none";
  if (view === "single") renderSingleView();
}

// ---------- all view ----------
function renderAllView(data) {
  const agg = data.aggregate || {};
  // chart
  if (chartAgg) { chartAgg.remove(); chartAgg = null; }
  const container = $("aggChart");
  container.innerHTML = "";
  chartAgg = LightweightCharts.createChart(container, {
    layout: { background: { type: "solid", color: "#0b0e14" }, textColor: "#dbe2ea", fontSize: 11 },
    grid: { vertLines: { color: "#161c26" }, horzLines: { color: "#161c26" } },
    timeScale: { visible: true, timeVisible: true, secondsVisible: false, borderColor: "#232c3b" },
    rightPriceScale: { borderColor: "#232c3b" },
    autoSize: true,
  });
  const series = chartAgg.addLineSeries({ color: "#4f9cf9", lineWidth: 2, priceLineVisible: false, lastValueVisible: true });
  const pts = (agg.equity || []).map(e => {
    const d = parseTs(e.ts);
    return d ? { time: Math.floor(d.getTime() / 1000), value: e.pnl } : null;
  }).filter(Boolean);
  if (pts.length) series.setData(pts);

  // agg metrics
  const host = $("aggMetrics");
  host.innerHTML = "";
  const metrics = [
    ["Strategies", agg.strategies || 0],
    ["Total Trades", agg.total_trades || 0],
    ["Total P&L", fmtPnl(agg.total_pnl || 0), pnlClass(agg.total_pnl || 0)],
    ["Max Drawdown", "-" + (agg.max_drawdown || 0).toFixed(2), "neg"],
  ];
  for (const m of metrics) {
    const box = document.createElement("div");
    box.className = "metricBox";
    box.innerHTML = `<span class="lbl">${m[0]}</span><span class="val ${m[2] || ''}">${m[1]}</span>`;
    host.appendChild(box);
  }

  // strategy table
  const stratCols = [
    { key: "sleeve", render: r => escapeHtml(r.sleeve) },
    { key: "trades" },
    { key: "wins" },
    { key: "win_rate", render: r => (r.win_rate || 0).toFixed(1) + "%" },
    { key: "total_pnl", render: r => fmtPnl(r.total_pnl || 0), cls: r => pnlClass(r.total_pnl || 0) },
    { key: "avg_win", render: r => (r.avg_win || 0).toFixed(2) },
    { key: "avg_loss", render: r => (r.avg_loss || 0).toFixed(2) },
    { key: "max_drawdown", render: r => "-" + (r.max_drawdown || 0).toFixed(2) },
    { key: "profit_factor", render: r => (r.profit_factor || 0).toFixed(2) },
  ];
  makeSortable("stratTable", "stratBody", data.strategies || [], stratCols);

  const tbody = $("stratBody");
  tbody.querySelectorAll("tr").forEach(tr => {
    tr.style.cursor = "pointer";
    tr.addEventListener("click", () => {
      $("viewSel").value = "single";
      $("stratSel").value = tr._rowData.sleeve;
      updateViewVisibility();
    });
  });

  // trade log
  const allTrades = [];
  for (const s of (data.strategies || [])) {
    for (const t of (s.trade_log || [])) {
      allTrades.push({ ...t, sleeve: s.sleeve });
    }
  }
  const tradeCols = [
    { key: "sleeve", render: r => escapeHtml(r.sleeve) },
    { key: "side", render: r => r.side, cls: r => r.side === "long" ? "pos" : "neg" },
    { key: "entry_ts", render: r => r.entry_ts ? parseTs(r.entry_ts).toLocaleString() : "-" },
    { key: "exit_ts", render: r => r.exit_ts ? parseTs(r.exit_ts).toLocaleString() : "-" },
    { key: "entry_px" },
    { key: "exit_px" },
    { key: "qty" },
    { key: "pnl", render: r => fmtPnl(r.pnl), cls: r => pnlClass(r.pnl) },
    { key: "tag", render: r => escapeHtml(r.tag) },
  ];
  makeSortable("tradeTable", "tradeBody", allTrades, tradeCols);
}

// ---------- bar chart ----------
function renderBarChart() {
  if (chartBar) { chartBar.remove(); chartBar = null; }
  const container = $("barChart");
  container.innerHTML = "";
  chartBar = LightweightCharts.createChart(container, {
    layout: { background: { type: "solid", color: "#0b0e14" }, textColor: "#dbe2ea", fontSize: 11 },
    grid: { vertLines: { color: "#161c26" }, horzLines: { color: "#161c26" } },
    timeScale: { visible: true, timeVisible: false, secondsVisible: false, borderColor: "#232c3b" },
    rightPriceScale: { borderColor: "#232c3b" },
    autoSize: true,
  });
  const series = chartBar.addHistogramSeries({ priceFormat: { type: "price", precision: 2 }, base: 0 });

  // collect trades from selected strategies
  let trades = [];
  if (perfData) {
    for (const s of (perfData.strategies || [])) {
      if (selectedStrats.has(s.sleeve)) {
        trades = trades.concat((s.trade_log || []).map(t => ({ ...t, sleeve: s.sleeve })));
      }
    }
  }

  const rows = aggregateByPeriod(trades, barPeriod);
  const data = rows.map(r => ({
    time: periodToTs(r.period, barPeriod),
    value: r.pnl,
    color: r.pnl >= 0 ? "rgba(38,166,154,0.8)" : "rgba(239,83,80,0.8)",
  })).filter(d => d.time > 0);
  if (data.length) series.setData(data);
}

// ---------- single view ----------
function renderSingleView() {
  if (!perfData) return;
  const sleeve = $("stratSel").value;
  const strat = (perfData.strategies || []).find(s => s.sleeve === sleeve);
  if (!strat) return;

  $("drillTitle").textContent = sleeve;

  const host = $("drillMetrics");
  host.innerHTML = "";
  const metrics = [
    ["Trades", strat.trades],
    ["Wins", strat.wins],
    ["Losses", strat.losses],
    ["Win Rate", (strat.win_rate || 0).toFixed(1) + "%"],
    ["Total P&L", fmtPnl(strat.total_pnl || 0), pnlClass(strat.total_pnl || 0)],
    ["Avg Win", (strat.avg_win || 0).toFixed(2), "pos"],
    ["Avg Loss", (strat.avg_loss || 0).toFixed(2), "neg"],
    ["Max DD", "-" + (strat.max_drawdown || 0).toFixed(2), "neg"],
    ["Profit Factor", (strat.profit_factor || 0).toFixed(2)],
  ];
  for (const m of metrics) {
    const box = document.createElement("div");
    box.className = "metricBox";
    box.innerHTML = `<span class="lbl">${m[0]}</span><span class="val ${m[2] || ''}">${m[1]}</span>`;
    host.appendChild(box);
  }

  if (chartDrill) { chartDrill.remove(); chartDrill = null; }
  const container = $("drillChart");
  container.innerHTML = "";
  chartDrill = LightweightCharts.createChart(container, {
    layout: { background: { type: "solid", color: "#0b0e14" }, textColor: "#dbe2ea", fontSize: 11 },
    grid: { vertLines: { color: "#161c26" }, horzLines: { color: "#161c26" } },
    timeScale: { visible: true, timeVisible: true, secondsVisible: false, borderColor: "#232c3b" },
    rightPriceScale: { borderColor: "#232c3b" },
    autoSize: true,
  });
  const series = chartDrill.addLineSeries({ color: "#4f9cf9", lineWidth: 2, priceLineVisible: false, lastValueVisible: true });
  const pts = (strat.equity || []).map(e => {
    const d = parseTs(e.ts);
    return d ? { time: Math.floor(d.getTime() / 1000), value: e.pnl } : null;
  }).filter(Boolean);
  if (pts.length) series.setData(pts);

  renderPeriodTable(strat.trade_log || []);

  const tradeCols = [
    { key: "side", render: r => r.side, cls: r => r.side === "long" ? "pos" : "neg" },
    { key: "entry_ts", render: r => r.entry_ts ? parseTs(r.entry_ts).toLocaleString() : "-" },
    { key: "exit_ts", render: r => r.exit_ts ? parseTs(r.exit_ts).toLocaleString() : "-" },
    { key: "entry_px" },
    { key: "exit_px" },
    { key: "qty" },
    { key: "pnl", render: r => fmtPnl(r.pnl), cls: r => pnlClass(r.pnl) },
    { key: "tag", render: r => escapeHtml(r.tag) },
  ];
  makeSortable("drillTradeTable", "drillTradeBody", strat.trade_log || [], tradeCols);
}

function renderPeriodTable(trades) {
  const rows = aggregateByPeriod(trades, currentPeriod);
  const cols = [
    { key: "period" },
    { key: "trades" },
    { key: "wins" },
    { key: "pnl", render: r => fmtPnl(r.pnl), cls: r => pnlClass(r.pnl) },
    { key: "cumulative", render: r => fmtPnl(r.cumulative), cls: r => pnlClass(r.cumulative) },
  ];
  makeSortable("periodTable", "periodBody", rows, cols);
}

// ---------- init ----------
async function init() {
  const syms = ["ES", "NQ", "GC"];
  const sel = $("symSel");
  for (const s of syms) {
    const o = document.createElement("option"); o.value = s; o.textContent = s;
    sel.appendChild(o);
  }
  const now = new Date();
  const from = new Date(now.getTime() - 30 * 86400000);
  $("toDate").value = now.toISOString().slice(0, 10);
  $("fromDate").value = from.toISOString().slice(0, 10);

  $("loadBtn").addEventListener("click", loadPerformance);
  $("viewSel").addEventListener("change", updateViewVisibility);
  $("stratSel").addEventListener("change", renderSingleView);

  document.querySelectorAll(".periodTabs button").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".periodTabs button").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      currentPeriod = btn.dataset.period;
      if (perfData) renderSingleView();
    });
  });

  $("barPeriodSel").addEventListener("change", (e) => {
    barPeriod = e.target.value;
    renderBarChart();
  });

  await loadPerformance();
}

document.addEventListener("DOMContentLoaded", init);
