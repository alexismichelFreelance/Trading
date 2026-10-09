"""StateSources — pull REAL engine state into publishable, serializable payloads.

This is the "never re-implement in JS" guarantee. Every payload the dashboard
draws comes from the live engine objects that actually computed it — nothing is
recomputed, approximated, or described-then-redrawn here. These functions only
READ: they do not mutate, do not feed detectors, and do not trigger anything.
They are the thin, dirty edge that turns engine internals into the stable
schema documented in engine/features/state_store.py.

WHAT THE ENGINE ACTUALLY COMPUTES TODAY (enumerated, with where/when — see the
trading-engine knowledge graph and engine/painters.py / level_book.py /
features/*):

    indicator      where it lives                        when it updates
  ─────────────    ─────────────────────────────────     ───────────────────
  1m bars         feed Bar events (core/events.Bar)      each closed 1m bar
  higher-TF bars  engine/features/bars.BarAggregator     when a bucket closes
  VWAP (+sigma)   features/avwap.AnchoredVWAP            every 1m bar
  floor pivots    features/pivots.SessionLevels /        when prior session
                  .MultiPivots (and LevelBook.piv)        rolls (day boundary)
  gamma walls     features/gamma_levels.GammaLevels /    once per session at
                  features/gamma_curve.GammaCurve         rollover (set_gamma)
  day range       features/day_range.DayRange            per price (RTH)
  S/D zones       features/zones ZoneDetector/ZoneBook   per 30m/1h/4h/1d bar +
                  + painters.ZoneView                    per-1m price touches
  target/stop     strategy.chart_marks() protocol        per bar / per fill
  open position   core.blotter TradeLedger.avg/qty       per fill

A CONSUMER SUBSCRIBES by feeding engine hooks into engine/adapters/state_publish
.StatePublisher: on_bar_hook, on_live_fill/on_paper_fill, on_session_rollover,
on_feed_disconnect/on_lane_stale, plus a logging.Handler. See that module.
"""
from __future__ import annotations

from ..core.timeutil import NS, et_session_date
from ..features.pivots import SessionLevels


def _session_ts(bar_ts: int) -> int:
    """Stable epoch-ns for the ET session day `bar_ts` belongs to (midnight ET),
    so static intraday grids (pivots/gamma) dedupe instead of accumulating a new
    duplicate point on every bar."""
    import pandas as pd
    day = et_session_date(bar_ts)
    return int(pd.Timestamp(day, tz="America/New_York").value)


def zones_from_view(zoneview, bar_ts: int, tfs: tuple = ("30m", "1h", "4h", "1d")):
    """Every current zone the engine's ZoneView holds, as ZoneDicts. READS the
    real Zone objects (geometry, direction, lifecycle status)."""
    out = []
    if zoneview is None:
        return out
    books = getattr(zoneview, "book", None) or {}
    for tf in tfs:
        book = books.get(tf)
        if book is None:
            continue
        for z in getattr(book, "zones", []) or []:
            if getattr(z, "broken", False):
                continue                      # current zones only; broken ones
            out.append({                       # are a lifecycle record, not a
                "id": f"{tf}:{z.formed_ts}:{z.direction}",  # live level
                "tf": tf,
                "top": round(float(z.top), 4),
                "bot": round(float(z.bot), 4),
                "direction": int(z.direction),   # +1 demand, -1 supply
                "virgin": bool(z.virgin),
                "broken": bool(z.broken),
                "touches": int(z.touches),
                "composite": int(z.composite),
                "formed_ts": int(z.formed_ts),
                "proximal": round(float(z.proximal()), 4),
                "distal": round(float(z.distal()), 4),
            })
    return out


def _round(x) -> float | None:
    if x is None or x != x:                    # None or NaN
        return None
    return round(float(x), 4)


def indicators_from_levelbook(levelbook, bar_ts: int) -> list:
    """Indicator point series from a LevelBook (the one object that holds the
    engine's merged pivots + VWAP + gamma + zones). Empty series are omitted."""
    out = []
    if levelbook is None:
        return out
    sess = _session_ts(bar_ts)

    # VWAP — a real time series: one point per bar close, accumulating the line.
    v = getattr(levelbook, "vwap", None)
    if v is not None:
        val = _round(v.value)
        if val is not None:
            out.append({"id": "vwap", "producer": "levelbook:avwap",
                        "label": "anchored VWAP", "unit": "price",
                        "points": [{"ts": int(bar_ts), "v": val}]})

    # Floor pivots — static intraday grid, stamped at the session.
    piv = getattr(getattr(levelbook, "piv", None), "pivots", None)
    if piv:
        out.append({"id": "pivots", "producer": "levelbook:pivots",
                    "label": "floor pivots", "unit": "price",
                    "points": [{"ts": sess, "v": float(px)} for px in piv]})

    # Gamma walls — static per session.
    g = getattr(levelbook, "gamma", None)
    if isinstance(g, dict):
        pts = []
        for k, label in (("put_wall", "put wall"), ("call_wall", "call wall"),
                         ("flip", "gamma flip")):
            x = _round(g.get(k))
            if x is not None:
                pts.append({"ts": sess, "v": x})
        if pts:
            out.append({"id": "gamma", "producer": "levelbook:gamma_levels",
                        "label": "dealer gamma", "unit": "price", "points": pts})
    return out


def positions_from_strategies(strategies, symbol: str, blotter=None) -> list:
    """Open positions per sleeve, with the entry/target/stop rails from the
    declared chart_marks() protocol. READS real state: only sleeves that
    implement chart_marks() contribute; nothing is guessed.

    `avg_px` is NOT read from the sleeve (real strategies carry `pos` and
    chart_marks but no `avg_px`): it is the AUTHORITATIVE average-cost entry
    taken from the blotter's ledger for the symbol when a `blotter` is given
    (see core/blotter.py TradeLedger). Without a blotter, avg_px stays None and
    the schema documents it as such — never a fabricated number."""
    out = []
    for s in strategies or []:
        marks = getattr(s, "chart_marks", None)
        marks = marks() if callable(marks) else {}
        qty = getattr(s, "pos", None) or getattr(s, "position", None)
        if qty is None:
            qty = 0
        avg = getattr(s, "avg_px", None)
        if avg is None and blotter is not None:
            # authoritative average-cost entry from the blotter ledger
            for sym, led in getattr(blotter, "_ledgers", {}).items():
                if sym == (getattr(s, "symbol", "") or symbol):
                    avg = led.avg_px if led.qty else None
                    break
        if not qty:
            # flat sleeves carry no mark; omit them so the dashboard isn't
            # cluttered with +0 rows (mirrors the panel's decision).
            continue
        out.append({
            "sleeve": getattr(s, "label", "") or type(s).__name__,
            "symbol": getattr(s, "symbol", "") or symbol,
            "qty": int(qty),
            "avg_px": _round(avg),
            "entry": _round(marks.get("entry")),
            "target": _round(marks.get("target")),
            "stop": _round(marks.get("stop")),
        })
    return out


def position_from_blotter(blotter, symbol: str, mark=None) -> list:
    """Open position per the blotter's average-cost ledger (authoritative for
    opened trades vs. a sleeve's own book), matched with chart_marks if a
    `mark` extractor is given. `mark` maps sleeve->{entry,target,stop}."""
    out = []
    if blotter is None:
        return out
    for sym, ledger in getattr(blotter, "_ledgers", {}).items():
        qty = ledger.qty
        if not qty:
            continue
        sd = (mark or {}).get("", {})
        out.append({
            "sleeve": "blotter",
            "symbol": sym,
            "qty": int(qty),
            "avg_px": _round(ledger.avg_px),
            "entry": _round(ledger.avg_px),
            "target": _round(sd.get("target")),
            "stop": _round(sd.get("stop")),
        })
    return out


__all__ = ["zones_from_view", "indicators_from_levelbook",
           "positions_from_strategies", "position_from_blotter", "_session_ts"]
