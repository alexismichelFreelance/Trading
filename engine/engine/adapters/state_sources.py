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
            # Keep broken zones too (marked broken) so the chart can show
            # lifecycle transitions; only the client decides how to style them.
            out.append({
                "id": f"{tf}:{z.formed_ts}:{z.direction}",
                "tf": tf,
                "top": round(float(z.top), 4),
                "bot": round(float(z.bot), 4),
                "direction": int(z.direction),   # +1 demand, -1 supply
                "virgin": bool(z.virgin),
                "broken": bool(z.broken),
                "touches": int(z.touches),
                "composite": int(z.composite),
                "formed_ts": int(z.formed_ts),
                "broken_ts": getattr(z, "broken_ts", None),
                "proximal": round(float(z.proximal()), 4),
                "distal": round(float(z.distal()), 4),
                "flipped_from": getattr(z, "flipped_from", None),
            })
    return out


def _round(x) -> float | None:
    if x is None or x != x:                    # None or NaN
        return None
    return round(float(x), 4)


def _vwap_bands(vwap, bar_ts: int) -> list:
    """The anchored-VWAP LINE plus its +-2s / +-3s band boundaries as point
    series. Every value is computed by the engine's AnchoredVWAP
    (.value / .sigma / .band(k)); JS only pairs hi/lo to draw filled bands.
    One point per bar, ts-stamped — the line accumulates across the session."""
    out = []
    if vwap is None:
        return out
    v = vwap.value
    if v is None or v != v:                    # no volume -> no line yet
        return out
    ts = int(bar_ts)
    out.append({"id": "vwap", "producer": "levelbook:avwap",
                "label": "anchored VWAP", "unit": "price",
                "points": [{"ts": ts, "v": _round(v)}]})
    # sigma is undefined until the band has accumulated enough data; emit
    # NOTHING for those bars (never a null point — LWC rejects null values)
    lo2, hi2 = vwap.band(2.0)
    if hi2 is not None and hi2 == hi2:
        out.append({"id": "vwap_2s_hi", "producer": "levelbook:avwap",
                    "label": "VWAP +2σ", "unit": "price",
                    "points": [{"ts": ts, "v": _round(hi2)}]})
    if lo2 is not None and lo2 == lo2:
        out.append({"id": "vwap_2s_lo", "producer": "levelbook:avwap",
                    "label": "VWAP −2σ", "unit": "price",
                    "points": [{"ts": ts, "v": _round(lo2)}]})
    lo3, hi3 = vwap.band(3.0)
    if hi3 is not None and hi3 == hi3:
        out.append({"id": "vwap_3s_hi", "producer": "levelbook:avwap",
                    "label": "VWAP +3σ", "unit": "price",
                    "points": [{"ts": ts, "v": _round(hi3)}]})
    if lo3 is not None and lo3 == lo3:
        out.append({"id": "vwap_3s_lo", "producer": "levelbook:avwap",
                    "label": "VWAP −3σ", "unit": "price",
                    "points": [{"ts": ts, "v": _round(lo3)}]})
    return out


def indicators_from_levelbook(levelbook, bar_ts: int) -> list:
    """Indicator point series from a LevelBook (the one object that holds the
    engine's merged pivots + VWAP + gamma + zones). Empty series are omitted."""
    out = []
    if levelbook is None:
        return out
    sess = _session_ts(bar_ts)

    # VWAP — a real time series (one point per bar close): the line + bands.
    out += _vwap_bands(getattr(levelbook, "vwap", None), bar_ts)

    # Floor pivots - static intraday grid, stamped at the session.
    piv = getattr(getattr(levelbook, "piv", None), "pivots", None)
    if piv:
        out.append({"id": "pivots", "producer": "levelbook:pivots",
                    "label": "floor pivots (daily)", "unit": "price",
                    "points": [{"ts": sess, "v": float(px), "label": "pivot"}
                               for px in piv]})

    # Multi-period floor pivots (D/W/M) - labelled grid, stamped at session.
    mpiv = getattr(levelbook, "mpiv", None)
    grid = getattr(mpiv, "grid", None)
    if callable(grid):
        try:
            g = grid() or {}
        except Exception:                       # noqa: BLE001
            g = {}
        if g:
            pts = [{"ts": sess, "v": float(px), "label": str(name),
                    "key": f"mpivot-{name}-{round(float(px), 2)}"}
                   for px, name in g.items()]
            out.append({"id": "mpivots", "producer": "levelbook:mpivots",
                        "label": "floor pivots (D/W/M)", "unit": "price",
                        "points": pts})

    # Gamma walls - static per session, one labelled point per level.
    g = getattr(levelbook, "gamma", None)
    if isinstance(g, dict):
        pts = []
        for k, label in (("put_wall", "put wall"), ("call_wall", "call wall"),
                         ("flip", "gamma flip")):
            x = _round(g.get(k))
            if x is not None:
                pts.append({"ts": sess, "v": x, "label": label,
                            "key": f"gamma-{k}"})
        if pts:
            out.append({"id": "gamma", "producer": "levelbook:gamma_levels",
                        "label": "dealer gamma", "unit": "price", "points": pts})
    return out


def positions_from_strategies(strategies, symbol: str, blotter=None, bar_ts: int = 0) -> list:
    """Open positions per sleeve, with the entry/target/stop rails from the
    declared chart_marks() protocol. READS real state: only sleeves that
    implement chart_marks() contribute; nothing is guessed.

    `avg_px` is NOT read from the sleeve (real strategies carry `pos` and
    chart_marks but no `avg_px`): it is the AUTHORITATIVE average-cost entry
    taken from the blotter's ledger for the symbol when a `blotter` is given
    (see core/blotter.py TradeLedger). Without a blotter, avg_px stays None and
    the schema documents it as such — never a fabricated number.

    `bar_ts` (epoch-ns) bounds the drawn level in time: start_ts = bar_ts - 1 day,
    end_ts = bar_ts (open-ended, extended by the client on each new bar)."""
    out = []
    start_ts = (bar_ts - 86_400_000_000_000) if bar_ts else None
    for s in strategies or []:
        marks = getattr(s, "chart_marks", None)
        marks = marks() if callable(marks) else {}
        qty = getattr(s, "pos", None) or getattr(s, "position", None)
        if qty is None:
            qty = 0
        avg = getattr(s, "avg_px", None)
        if avg is None and blotter is not None:
            for sym, led in getattr(blotter, "_ledgers", {}).items():
                if sym == (getattr(s, "symbol", "") or symbol):
                    avg = led.avg_px if led.qty else None
                    break
        if not qty:
            continue
        out.append({
            "sleeve": getattr(s, "label", "") or type(s).__name__,
            "symbol": getattr(s, "symbol", "") or symbol,
            "qty": int(qty),
            "avg_px": _round(avg),
            "entry": _round(marks.get("entry")),
            "target": _round(marks.get("target")),
            "stop": _round(marks.get("stop")),
            "start_ts": start_ts,
            "end_ts": bar_ts if bar_ts else None,
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


def _curve_from(gamma_curve) -> list:
    """The full dealer-gamma curve as [[strike, net_gex, cum_gex], ...] straight
    from the engine's GammaCurve profile (not every use needs it, but it is
    drawable and costs nothing to publish)."""
    prof = getattr(gamma_curve, "_prof", {}) or {}
    curve = prof.get("curve") or []
    return [[_round(k), _round(n), _round(c)] for k, n, c in curve]


def _n_strikes(gamma_curve) -> int:
    prof = getattr(gamma_curve, "_prof", {}) or {}
    return int(len(prof.get("curve") or []))


def gamma_map_from(gamma_curve, levelbook, price: float) -> dict | None:
    """The structured dealer-gamma map at `price` (the LIVE price — so the
    regime is answered WHERE PRICE IS, and toggles as price crosses a flip
    during the day). Reads the real GammaCurve (multi flips, ranked walls,
    pocket, the full curve); falls back to the flat LevelBook dict.

    Returns None only when there is no gamma source at all (fail-open)."""
    if gamma_curve is not None and getattr(gamma_curve, "ok", False):
        at = gamma_curve.at(price)
        if at is None:
            return None
        return {
            "kind": "curve",
            "ok": True,
            "sess": getattr(gamma_curve, "sess", ""),
            "underlying": getattr(gamma_curve, "underlying", ""),
            "basis": _round(getattr(gamma_curve, "basis", 0.0)),
            "price": _round(price),
            "flips": [_round(f) for f in at.get("flips", [])],
            "flip": _round(at.get("flip")),
            "call_walls": [[_round(k), None] for k in at.get("call_walls", [])][:5],
            "put_walls": [[_round(k), None] for k in at.get("put_walls", [])][:5],
            "walls": [[_round(k), _round(n)] for k, n in at.get("walls", [])][:8],
            "pocket": [_round(p) for p in at.get("pocket", (None, None))],
            "local_sign": int(at.get("local_sign", 0)),
            "total_sign": int(at.get("total_sign", 0)),
            "net_at_price": _round(at.get("cum")),
            "dist_to_edge": _round(gamma_curve.dist_to_edge(price)),
            "curve": _curve_from(gamma_curve),
            "n_strikes": _n_strikes(gamma_curve),
        }
    # FLAT fallback: the daily row (one put wall, one call wall, one flip).
    g = getattr(levelbook, "gamma", None) if levelbook else None
    if isinstance(g, dict):
        return {
            "kind": "flat",
            "ok": bool(g.get("put_wall") or g.get("call_wall") or g.get("flip")),
            "sess": g.get("sess", ""),
            "underlying": g.get("underlying", ""),
            "basis": _round(g.get("basis")),
            "price": _round(price),
            "flips": [_round(g["flip"])] if g.get("flip") is not None else [],
            "flip": _round(g.get("flip")),
            "call_walls": [[_round(g["call_wall"]), None]] if g.get("call_wall") else [],
            "put_walls": [[_round(g["put_wall"]), None]] if g.get("put_wall") else [],
            "walls": [],
            "pocket": [None, None],
            "local_sign": int(g.get("net_sign", 0)),
            "total_sign": int(g.get("net_sign", 0)),
            "net_at_price": None,
            "dist_to_edge": None,
            "curve": [],
            "n_strikes": 0,
        }
    return None


def strategies_roster_from(strategies, bar_ts: int = 0) -> list:
    """The engine's STRATEGY ROSTER (labels + enabled flags + per-strategy
    chart marks), as shown in the dashboard's strategy list with show/hide.

    Reads the real strategy objects on the lane: each entry is one engine
    strategy with its roster `label`, whether it is `enabled`, its open
    `qty`/`entry_px`, and the declared chart_marks() overlay it draws on the
    chart (entry / target / stop). A strategy that is flat carries no mark and
    contributes its name only — its toggle still shows/hides its category.

    `bar_ts` (epoch-ns) bounds drawn levels: start_ts = bar_ts - 1 day,
    end_ts = bar_ts (extended by the client on each new bar)."""
    out = []
    start_ts = (bar_ts - 86_400_000_000_000) if bar_ts else None
    for s in strategies or []:
        marks = getattr(s, "chart_marks", None)
        marks = marks() if callable(marks) else {}
        qty = getattr(s, "pos", None) or getattr(s, "position", None) or 0
        out.append({
            "label": getattr(s, "label", "") or type(s).__name__,
            "enabled": bool(getattr(s, "enabled", True)),
            "mode": getattr(s, "mode", "paper"),
            "qty": int(qty),
            "entry_px": _round(getattr(s, "entry_px", None)),
            "entry": _round(marks.get("entry")),
            "target": _round(marks.get("target")),
            "stop": _round(marks.get("stop")),
            "start_ts": start_ts,
            "end_ts": bar_ts if bar_ts else None,
        })
    return out


__all__ = ["zones_from_view", "indicators_from_levelbook", "gamma_map_from",
           "strategies_roster_from", "positions_from_strategies",
           "position_from_blotter", "_session_ts"]
