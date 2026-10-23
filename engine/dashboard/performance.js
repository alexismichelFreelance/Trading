"use strict";
const $ = (id) => document.getElementById(id);
const sec = (ns) => Math.floor(ns / 1e9);

let chart = null;
let aggSeries = null;

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
    render(data);
    $("loading").style.display = "none";
    $("content").style.display = "block";
  } catch (e) {
    $("loading").textContent = "Failed to load: " + e.message;
  }
}

function fmtPnl(v) { return (v >= 0 ? "+" : "") + v.toFixed(2); }
function pnlClass(v) { return v >= 0 ? "pos" : "neg"; }

function render(data) {
  const agg = data.aggregate || {};
  // equity curve
  if (chart) { chart.remove(); chart = null; }
  const container = $("aggChart");
  container.innerHTML = "";
  chart = LightweightCharts.createChart(container, {
    layout: { background: { type: "solid", color: "#0b0e14" }, textColor: "#dbe2ea", fontSize: 11 },
    grid: { vertLines: { color: "#161c26" }, horzLines: { color: "#161c26" } },
    timeScale: { visible: true, timeVisible: true, secondsVisible: false, borderColor: "#232c3b" },
    rightPriceScale: { borderColor: "#232c3b" },
    autoSize: true,
  });
  aggSeries = chart.addLineSeries({ color: "#4f9cf9", lineWidth: 2, priceLineVisible: false, lastValueVisible: true });
  const pts = (agg.equity || []).map(e => {
    const d = new Date(String(e.ts).replace(" ", "T"));
    return { time: Math.floor(d.getTime() / 1000), value: e.pnl };
  }).filter(p => !isNaN(p.time));
  if (pts.length) aggSeries.setData(pts);

  // aggregate metrics
  const aggHost = $("aggMetrics");
  aggHost.innerHTML = "";
  aggHost.appendChild(metricCard("Aggregate", [
    ["Strategies", agg.strategies || 0],
    ["Total Trades", agg.total_trades || 0],
    ["Total P&L", fmtPnl(agg.total_pnl || 0), pnlClass(agg.total_pnl || 0)],
    ["Max Drawdown", "-" + (agg.max_drawdown || 0).toFixed(2), "neg"],
  ]));

  // per-strategy metrics
  const stratHost = $("stratMetrics");
  stratHost.innerHTML = "";
  for (const s of (data.strategies || [])) {
    stratHost.appendChild(metricCard(s.sleeve, [
      ["Trades", s.trades],
      ["Wins", s.wins],
      ["Losses", s.losses],
      ["Win Rate", (s.win_rate || 0).toFixed(1) + "%"],
      ["Total P&L", fmtPnl(s.total_pnl || 0), pnlClass(s.total_pnl || 0)],
      ["Avg Win", (s.avg_win || 0).toFixed(2), "pos"],
      ["Avg Loss", (s.avg_loss || 0).toFixed(2), "neg"],
      ["Max DD", "-" + (s.max_drawdown || 0).toFixed(2), "neg"],
      ["Profit Factor", (s.profit_factor || 0).toFixed(2)],
    ]));
  }

  // trade log
  const tbody = $("tradeBody");
  tbody.innerHTML = "";
  const allTrades = [];
  for (const s of (data.strategies || [])) {
    for (const t of (s.trade_log || [])) {
      allTrades.push({ ...t, sleeve: s.sleeve });
    }
  }
  allTrades.sort((a, b) => new Date(b.exit_ts) - new Date(a.exit_ts));
  for (const t of allTrades.slice(0, 200)) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(t.sleeve)}</td>
      <td class="${t.side === 'long' ? 'pos' : 'neg'}">${t.side}</td>
      <td>${t.entry_ts ? new Date(String(t.entry_ts).replace(" ", "T")).toLocaleString() : '-'}</td>
      <td>${t.exit_ts ? new Date(String(t.exit_ts).replace(" ", "T")).toLocaleString() : '-'}</td>
      <td>${t.entry_px}</td>
      <td>${t.exit_px}</td>
      <td>${t.qty}</td>
      <td class="${pnlClass(t.pnl)}">${fmtPnl(t.pnl)}</td>
      <td>${escapeHtml(t.tag)}</td>`;
    tbody.appendChild(tr);
  }
}

function metricCard(title, rows) {
  const card = document.createElement("div");
  card.className = "metricCard";
  const h = document.createElement("h4");
  h.textContent = title;
  card.appendChild(h);
  for (const r of rows) {
    const row = document.createElement("div");
    row.className = "metricRow";
    const lbl = document.createElement("span");
    lbl.className = "lbl";
    lbl.textContent = r[0];
    const val = document.createElement("span");
    val.className = "val" + (r[2] ? " " + r[2] : "");
    val.textContent = r[1];
    row.appendChild(lbl);
    row.appendChild(val);
    card.appendChild(row);
  }
  return card;
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// populate symbol selector
async function init() {
  const syms = ["ES", "NQ", "GC"];
  const sel = $("symSel");
  for (const s of syms) {
    const o = document.createElement("option"); o.value = s; o.textContent = s;
    sel.appendChild(o);
  }
  // default dates: last 30 days
  const now = new Date();
  const from = new Date(now.getTime() - 30 * 86400000);
  $("toDate").value = now.toISOString().slice(0, 10);
  $("fromDate").value = from.toISOString().slice(0, 10);
  $("loadBtn").addEventListener("click", loadPerformance);
  await loadPerformance();
}

document.addEventListener("DOMContentLoaded", init);
