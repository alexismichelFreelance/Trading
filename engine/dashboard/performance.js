"use strict";
const $ = (id) => document.getElementById(id);

let chartAgg = null;
let chartDrill = null;
let chartBar = null;
let perfData = null;
let currentPeriod = "day";
let barPeriod = "day";
let selectedStrats = new Set();

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
  // wire up checkboxes after render
  tbody.querySelectorAll(".stratCb").forEach(cb => {
    cb.addEventListener("change", () => {
      const sleeve = cb.dataset.sleeve;
      if (cb.checked) selectedStrats.add(sleeve);
      else selectedStrats.delete(sleeve);
      renderBarChart();
    });
  });
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
    // select all strategies by default
    selectedStrats = new Set((data.strategies || []).map(s => s.sleeve));
    populateStratSelector(data);
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

function updateViewVisibility() {
  const view = $("viewSel").value;
  $("stratLabel").style.display = view === "single" ? "" : "none";
  $("allView").style.display = view === "all" ? "block" : "none";
  $("singleView").style.display = view === "single" ? "block" : "none";
  if (view === "single") renderSingleView();
}

function getFilteredStrategies() {
  if (!perfData) return [];
  const modeFilter = $("modeFilterSel") ? $("modeFilterSel").value : "all";
  let strats = perfData.strategies || [];
  if (modeFilter === "live") strats = strats.filter(s => s.mode === "live");
  if (modeFilter === "paper") strats = strats.filter(s => s.mode === "paper");
  return strats;
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
  // dedup timestamps
  const seen = new Set();
  const pts = (agg.equity || []).map(e => {
    const d = parseTs(e.ts);
    if (!d) return null;
    const t = Math.floor(d.getTime() / 1000);
    if (seen.has(t)) return null;
    seen.add(t);
    return { time: t, value: e.pnl };
  }).filter(Boolean).sort((a, b) => a.time - b.time);
  // equity curve rendered
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

  // select all checkbox
  const selectAll = $("selectAllStrats");
  if (selectAll) {
    selectAll.checked = true;
    selectAll.addEventListener("change", () => {
      document.querySelectorAll(".stratCb").forEach(cb => cb.checked = selectAll.checked);
      selectedStrats = new Set();
      if (selectAll.checked) {
        getFilteredStrategies().forEach(s => selectedStrats.add(s.sleeve));
      }
      renderBarChart();
    });
  }

  // strategy table
  const strats = getFilteredStrategies();
  const stratCols = [
    { key: "sleeve", render: r => `<input type="checkbox" class="stratCb" data-sleeve="${escapeHtml(r.sleeve)}" ${selectedStrats.has(r.sleeve) ? 'checked' : ''}> ${escapeHtml(r.sleeve)}` },
    { key: "mode", render: r => `<span class="modeTag ${r.mode === 'live' ? 'live' : 'paper'}">${r.mode === 'live' ? 'LIVE' : 'PAPER'}</span>` },
    { key: "trades" },
    { key: "wins" },
    { key: "win_rate", render: r => (r.win_rate || 0).toFixed(1) + "%" },
    { key: "total_pnl", render: r => fmtPnl(r.total_pnl || 0), cls: r => pnlClass(r.total_pnl || 0) },
    { key: "avg_win", render: r => (r.avg_win || 0).toFixed(2) },
    { key: "avg_loss", render: r => (r.avg_loss || 0).toFixed(2) },
    { key: "max_drawdown", render: r => "-" + (r.max_drawdown || 0).toFixed(2) },
    { key: "profit_factor", render: r => (r.profit_factor || 0).toFixed(2) },
  ];
  makeSortable("stratTable", "stratBody", strats, stratCols);

  // click row to drill down (on sleeve name, not checkbox)
  const tbody = $("stratBody");
  tbody.querySelectorAll("tr").forEach(tr => {
    tr.style.cursor = "pointer";
    tr.addEventListener("click", (e) => {
      if (e.target.classList.contains("stratCb")) return;
      const sleeve = tr._rowData.sleeve;
      $("viewSel").value = "single";
      $("stratSel").value = sleeve;
      updateViewVisibility();
    });
  });

  // mode filter
  const modeSel = $("modeFilterSel");
  if (modeSel) {
    modeSel.addEventListener("change", () => {
      renderAllView(data);
      renderBarChart();
    });
  }

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
    ["Mode", strat.mode === "live" ? "LIVE" : "PAPER"],
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
  const seen = new Set();
  const pts = (strat.equity || []).map(e => {
    const d = parseTs(e.ts);
    if (!d) return null;
    const t = Math.floor(d.getTime() / 1000);
    if (seen.has(t)) return null;
    seen.add(t);
    return { time: t, value: e.pnl };
  }).filter(Boolean).sort((a, b) => a.time - b.time);
  // equity curve rendered
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

  const barPeriodSel = $("barPeriodSel");
  if (barPeriodSel) {
    barPeriodSel.addEventListener("change", (e) => {
      barPeriod = e.target.value;
      renderBarChart();
    });
  }

  const modeFilterSel = $("modeFilterSel");
  if (modeFilterSel) {
    modeFilterSel.addEventListener("change", () => {
      if (perfData) {
        renderAllView(perfData);
        renderBarChart();
      }
    });
  }

  await loadPerformance();
}

document.addEventListener("DOMContentLoaded", init);
