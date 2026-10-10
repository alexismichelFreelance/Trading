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

    # ── wiring ───────────────────────────────────────────────────────────
    def attach(self, engine) -> None:
        """Hook the engine's existing observer callbacks (the same ones the
        painter uses) into this publisher. Non-mutating to engines trading path:
        these are side-effect sinks, drained off the hot path."""
        self._engine = engine
        engine.on_bar_hook = self._on_bar
        engine.on_live_fill = self._on_fill
        engine.on_paper_fill = self._on_fill
        engine.on_session_rollover = self._on_session
        engine.on_feed_disconnect = self._on_disconnect
        engine.on_lane_stale = self._on_lane_stale
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
        src = self.lanes.get(sym)
        if src is None:
            return
        for ind in indicators_from_levelbook(src.levelbook, bar["ts"]):
            self.broadcast(self.store.apply_indicator(sym, ind))
        # structured dealer-gamma map at the LIVE close (regime crosses flips)
        price = bar["c"]
        gm = gamma_map_from(src.gamma_curve, src.levelbook, price)
        d = self.store.apply_gamma(sym, gm)
        if d is not None:
            self.broadcast(d)
        # strategy roster with show/hide + per-strategy marks
        d = self.store.apply_roster(sym, strategies_roster_from(src.strategies))
        if d is not None:
            self.broadcast(d)
        zones = zones_from_view(src.zoneview, bar["ts"])
        self.broadcast(self.store.apply_zones(sym, zones))
        await self._publish_positions(sym)

    async def _publish_positions(self, sym: str) -> None:
        src = self.lanes.get(sym)
        if src is None:
            return
        blotter = getattr(self._engine, "blotter", None) if self._engine else None
        for pos in positions_from_strategies(src.strategies, sym, blotter):
            d = self.store.apply_position(pos)
            if d is not None:
                self.broadcast(d)

    # ── observable surface (transport-free) ──────────────────────────────
    def snapshot(self) -> dict:
        return self.store.snapshot()

    # ── subscriber fan-out ───────────────────────────────────────────────
    def broadcast(self, delta: dict) -> None:
        """Put a delta to every subscriber queue, dropping on overflow so a
        slow consumer can never apply backpressure to the engine (same rule as
        the engine's own sink queue)."""
        for q in list(self._subs.values()):
            if q.full():
                q.get_nowait()          # drop oldest, keep the newest flowing
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
