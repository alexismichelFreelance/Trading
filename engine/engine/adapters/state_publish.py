"""StatePublisher — the DIRTY engine-side state-publish adapter (no UI, no
transport). Turns the engine's real computation into a clean, queryable,
subscribable form a dashboard (and later a local WebSocket) binds to.

DESIGN
  - Decoupled from transport: the only observable surface is `snapshot()` and
    `subscribe()`, both returning/emitting plain JSON-serializable dicts. A
    WebSocket layer later wraps `subscribe()`; nothing here knows about sockets.
  - Subscribes to the engine's OWN event flow via the hooks the engine already
    exposes (on_bar_hook, on_live_fill/on_paper_fill, on_session_rollover,
    on_feed_disconnect/on_lane_stale) plus a logging.Handler — the exact same
    observer pattern tools/run_live.py uses to drive the NT8 painter.
  - Pulls REAL state through engine/adapters/state_sources.py (zones from
    ZoneView, indicators from LevelBook, positions from chart_marks) so the
    dashboard draws the engine's actual computed values — never a redraw of
    them ("never re-implement in JS").
  - Pure heartbeat: engine/features/state_store.StateStore owns the schema and
    the snapshot/delta semantics; this adapter only feeds it and relays deltas.

TIME FRAME DECISION: the engine's canonical bar stream is the closed 1m Bar.
This adapter retains the raw 1m bars (source of truth) and ALSO aggregates to
the configured dashboard time frame(s) using the engine's own features/bars
.BarAggregator — so higher-TF candles are built engine-side with the exact
engine aggregation, never client-side. Default agg TF: 5m (raw 1m also served).

# ── 5-line README: state schema the dashboard consumes blind ──────────
# snapshot(): {"seq","ts","lanes":{sym:{bars_raw,bars{tf},indicators[],
#   zones[],positions[]}},"log","errors"} — see features/state_store.py.
# bars_raw   : closed 1m bars {ts,tf,o,h,l,c,v,complete}; bars{tf} = engine
#              aggregated candles (BarAggregator) at the configured TF.
# indicators[]: {id,producer,label,unit,points[{ts,v}]} — vwap (per bar),
#              pivots/gamma (static intraday grid, per session rollover).
# zones[]    : {id,tf,top,bot,direction,virgin,broken,touches,composite,
#              formed_ts,proximal,distal} — the engine ZoneBook, per bar.
# positions[]: {sleeve,symbol,qty,avg_px,entry,target,stop} — avg_px is
#   the blotter's authoritative entry, entry/target/stop from chart_marks();
#   log/errors[] = engine log + error surface.
# deltas     : {"seq","type","lane","payload"} type in {bar,indicator,zone,
#              position,fill,log,error,session,lane}; snapshot sent on connect.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field

from ..core.events import Bar, Fill
from ..features.bars import BarAggregator
from ..features.state_store import StateStore
from .state_sources import (gamma_map_from, indicators_from_levelbook,
                            positions_from_strategies, strategies_roster_from,
                            zones_from_view)

log = logging.getLogger("engine.state_publish")

NS = 1_000_000_000


def _combine(prev, own):
    """Chain a previously-attached async sink (the NT8 painter's) with our own
    so BOTH receive the engine event. The painter and this publisher are both
    observers of the same engine hooks; the LAST one to be assigned (us, after
    run_live.py wires the painter) must not silently steal them from the first.

    Prior sink runs FIRST, then our own work. An exception from the prior sink
    must never stop publication (fail-open, the same rule the engine's sink
    path already applies), and synchronous sinks are awaited transparently so
    a mix of coroutine and plain functions composes cleanly."""
    if prev is None:
        return own

    async def _both(*args, **kwargs):
        try:
            r = prev(*args, **kwargs)
            if asyncio.iscoroutine(r):
                await r
        except Exception:                             # noqa: BLE001
            pass
        await own(*args, **kwargs)

    return _both


@dataclass
class LaneSource:
    """The live engine objects whose state a lane publishes. Anything left None
    is simply omitted from that lane's snapshot (fail-open, like the painter)."""
    symbol: str
    zoneview: object = None          # painters.ZoneView  (zones)
    levelbook: object = None         # features.level_book.LevelBook (indicators)
    gamma_curve: object = None       # features.gamma_curve.GammaCurve (multi-wall)
    strategies: list = field(default_factory=list)   # chart_marks() -> rails
    color: str = ""


def _bar_dict(b: Bar) -> dict:
    return {"ts": int(b.ts), "tf": b.tf, "o": float(b.o), "h": float(b.h),
            "l": float(b.l), "c": float(b.c), "v": int(b.v),
            "symbol": b.symbol or "", "complete": bool(getattr(b, "complete", True))}


def _fill_dict(f: Fill) -> dict:
    return {"ts": int(f.ts), "order_id": f.order_id, "symbol": f.symbol or "",
            "price": float(f.price), "size": int(f.size), "tag": f.tag}


class _LogEmit(logging.Handler):
    """Bridges the stdlib logging stream (the engine log) into the store."""

    def __init__(self, store: StateStore, broadcast):
        super().__init__(level=logging.DEBUG)
        self.store = store
        self._broadcast = broadcast
        self.setFormatter(logging.Formatter("%(message)s"))
        # Attach ONCE to the root "engine" logger. Records from child namespaces
        # (engine.live, engine.blotter, ...) propagate up to "engine", and stdlib
        # logging.callHandlers fires the whole ancestor chain — so attaching to
        # "engine" AND to children made every record hit this handler TWICE.
        # One attach on the parent yields exactly one emit per record.
        logging.getLogger("engine").addHandler(self)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = record.levelname
            message = record.getMessage()
            if not message:
                return
            delta = self.store.apply_log({
                "ts": int(record.created * 1e9),
                "level": level,
                "message": message,
                "logger": record.name,
            })
            self._broadcast(delta)
        except Exception:                     # noqa: BLE001 - never break logging
            pass


class StatePublisher:
    """One publisher per engine (multi-lane aware). Attach to an engine and
    start a run; subscribers receive a snapshot-on-connect then live deltas."""

    def __init__(self, lanes, agg_timeframes=("5m",), max_bars=300) -> None:
        self.lanes: dict[str, LaneSource] = {
            (l.symbol if isinstance(l, LaneSource)
             else l.get("symbol") or l.get("lane")): (l if isinstance(l, LaneSource)
                                                      else LaneSource(**l))
            for l in lanes
        }
        self.agg_tfs = tuple(agg_timeframes)
        self.store = StateStore()
        self._aggs: dict[str, BarAggregator] = {
            sym: BarAggregator(self.agg_tfs) for sym in self.lanes}
        self._engine = None
        self._log_h = None
        self._subs: dict[str, asyncio.Queue] = {}
        self._started = False
        self._seeding = False

    # ── wiring ───────────────────────────────────────────────────────────
    def attach(self, engine) -> None:
        """Hook the engine's existing observer callbacks (the same ones the
        painter uses) into this publisher. Non-mutating to engines trading path:
        these are side-effect sinks, drained off the hot path. Any sink already
        attached (e.g. run_live.py's painter) is CHAINED, not replaced — both
        keep receiving events (see `_combine`)."""
        self._engine = engine
        engine.on_bar_hook = _combine(engine.on_bar_hook, self._on_bar)
        engine.on_live_fill = _combine(engine.on_live_fill, self._on_fill)
        engine.on_paper_fill = _combine(engine.on_paper_fill, self._on_fill)
        engine.on_live_order = _combine(getattr(engine, "on_live_order", None),
                                        self._on_signal)
        engine.on_warmup_signal = _combine(getattr(engine, "on_warmup_signal", None),
                                           self._on_warmup_signal)
        engine.on_session_rollover = _combine(engine.on_session_rollover,
                                              self._on_session)
        engine.on_feed_disconnect = _combine(engine.on_feed_disconnect,
                                             self._on_disconnect)
        engine.on_lane_stale = _combine(engine.on_lane_stale,
                                        self._on_lane_stale)
        self._log_h = _LogEmit(self.store, self.broadcast)
        self._started = True

    async def close(self) -> None:
        if self._log_h is not None:
            logging.getLogger("engine").removeHandler(self._log_h)
            self._log_h = None
        self._started = False

    # ── engine hook handlers ─────────────────────────────────────────────
    async def _on_bar(self, bar: Bar, live: bool, backfill: int) -> None:
        sym = getattr(bar, "symbol", "") or ""
        bd = _bar_dict(bar)
        delta = self.store.apply_bar(bd)
        self.broadcast(delta)
        agg = self._aggs.get(sym)
        if agg is not None:
            for ab in agg.update(bar):
                d = self.store.apply_agg_bar(_bar_dict(ab))
                if d is not None:
                    self.broadcast(d)
        await self._publish_lane(sym, bd)

    async def _on_fill(self, f: Fill) -> None:
        sym = f.symbol or ""
        self.broadcast(self.store.apply_fill(_fill_dict(f)))
        await self._publish_positions(sym)

    async def _on_signal(self, ts: int, side: int, qty: int, tag: str, px: float) -> None:
        sym = getattr(self._engine, "symbol", "") if self._engine else ""
        # best-effort symbol from the engine's lanes
        if not sym and self.lanes:
            sym = next(iter(self.lanes))
        rec = {"ts": int(ts), "symbol": sym, "side": int(side),
               "qty": int(qty), "tag": str(tag), "px": float(px) if px else None,
               "source": "live", "reduce_only": False}
        self.broadcast(self.store.apply_signal(rec))

    async def _on_warmup_signal(self, ts: int, side: int, qty: int, tag: str,
                                 px: float, symbol: str = "") -> None:
        sym = symbol or (next(iter(self.lanes)) if self.lanes else "")
        rec = {"ts": int(ts), "symbol": sym, "side": int(side),
               "qty": int(qty), "tag": str(tag), "px": float(px) if px else None,
               "source": "warmup", "reduce_only": False}
        self.broadcast(self.store.apply_signal(rec))

    async def _on_session(self, day: str) -> None:
        self.broadcast(self.store.apply_session(day))

    async def _on_disconnect(self, name: str, attempt: int, got: int) -> None:
        self.broadcast(self.store.apply_lane(
            name or "", {"action": "disconnect", "attempt": attempt, "got": got}))

    async def _on_lane_stale(self, lane: str, seconds: int) -> None:
        self.broadcast(self.store.apply_lane(
            lane, {"action": "stale", "seconds": seconds}))

    # ── pull real state per lane ─────────────────────────────────────────
    async def _publish_lane(self, sym: str, bar: dict) -> None:
        """Publish this lane's newly-computed state. Per series we broadcast
        ONLY the points the store had not already seen (append-only deltas),
        never a full historical array — the engine-side fix for the
        self-DOSing \"resend the whole history every bar\" flow. A series with
        nothing new returns None from the store and is skipped entirely."""
        src = self.lanes.get(sym)
        if src is None:
            return
        bar_ts = bar["ts"]
        # REPLAY GUARD. A bar OLDER than the lane's own indicator history is
        # a replay, not new history: the NT8 relay re-sends its startup
        # backfill after the dashboard lane was seeded with real history, and
        # the values a book holds NOW are the values at the LIVE edge.
        # Stamping them at the replay's timestamps draws today's VWAP over
        # last week's chart -- and where the seeded history and the replay
        # cover the same minute, two different values at one timestamp, which
        # is exactly what the chart library rejects (blank series).
        floor = self.store.indicator_floor(sym)
        for ind in indicators_from_levelbook(src.levelbook, bar_ts):
            # Static level series (pivots, gamma walls) are stamped at the
            # SESSION ts, not at bar_ts. The replay guard (which checks
            # bar_ts against the floor) must not gate them, otherwise a
            # replayed bar older than the floor suppresses the session's
            # pivot/gamma publication forever and the chart loses them.
            is_static = ("pivot" in (ind.get("producer") or "")
                         or "gamma_levels" in (ind.get("producer") or ""))
            if (not is_static) and bar_ts < floor:
                log.debug("REPLAY GUARD: bar_ts=%d < floor=%d for %s, skipping %s",
                          bar_ts, floor, sym, ind["id"])
                continue
            d = self.store.apply_indicator(sym, ind)
            if d is not None:
                log.debug("PUBLISH indicator %s for %s (%d pts)", ind["id"], sym, len(ind.get("points", [])))
                self.broadcast(d)
            else:
                log.debug("SKIP indicator %s for %s (no new points)", ind["id"], sym)
        price = bar["c"]
        gm = gamma_map_from(src.gamma_curve, src.levelbook, price)
        d = self.store.apply_gamma(sym, gm)
        if d is not None:
            self.broadcast(d)
        d = self.store.apply_roster(sym, strategies_roster_from(src.strategies, bar_ts))
        if d is not None:
            self.broadcast(d)
        zones = zones_from_view(src.zoneview, bar_ts)
        self.broadcast(self.store.apply_zones(sym, zones))
        await self._publish_positions(sym, bar_ts)

    async def _publish_positions(self, sym: str, bar_ts: int = 0) -> None:
        src = self.lanes.get(sym)
        if src is None:
            return
        blotter = getattr(self._engine, "blotter", None) if self._engine else None
        for pos in positions_from_strategies(src.strategies, sym, blotter, bar_ts):
            d = self.store.apply_position(pos)
            if d is not None:
                self.broadcast(d)

    # ── observable surface (transport-free) ──────────────────────────────
    def snapshot(self) -> dict:
        return self.store.snapshot()

    # ── subscriber fan-out ───────────────────────────────────────────────
    def broadcast(self, delta: dict) -> None:
        if delta is None or self._seeding:
            return
        for q in list(self._subs.values()):
            if q.full():
                q.get_nowait()
            else:
                q.put_nowait(delta)

    def subscribe(self, kinds: set | None = None, backlog: int = 512):
        """Async iterator: yields the full snapshot first, then live deltas.
        `kinds` (optional) filters which delta types are relayed. Transport-free
        — a WebSocket later just streams these dicts."""
        kinds = kinds or None
        q: asyncio.Queue = asyncio.Queue(maxsize=backlog)
        sid = uuid.uuid4().hex
        self._subs[sid] = q
        return self._subscriber(sid, q, kinds)

    async def _subscriber(self, sid: str, q: asyncio.Queue, kinds):
        try:
            yield {"type": "__snapshot__", "lane": "",
                   "payload": self.snapshot()}
            while True:
                delta = await q.get()
                if kinds and delta["type"] not in kinds:
                    continue
                yield delta
        finally:
            self._subs.pop(sid, None)

    def __repr__(self) -> str:
        return (f"<StatePublisher lanes={sorted(self.lanes)} "
                f"agg={self.agg_tfs} seq={self.store.seq}>")


__all__ = ["StatePublisher", "LaneSource"]
