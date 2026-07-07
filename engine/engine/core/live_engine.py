"""LiveEngine — the async live/paper driver.

Concurrently consumes `feed.stream()` (market events) and `broker.events()`
(fills/positions), merging them into a single queue so the strategy state is
mutated by exactly one consumer at a time (no locks). Uses the WallClock. The
dispatch of events to strategies is the SAME `dispatch_market`/`dispatch_broker`
as ReplayEngine — the central invariant holds: identical Strategy objects,
identical routing; only feed/broker differ.

Reconnection is handled per-adapter: a pump task that hits an error backs off
and re-enters the adapter's stream (the adapter re-establishes its socket). A
cleanly-exhausted feed (e.g. a bounded test feed) drains pending broker events
and stops.
"""
from __future__ import annotations

import asyncio
import logging

from .blotter import Blotter
from .dispatch import dispatch_broker, dispatch_market
from .events import Bar, Fill, PositionUpdate, Trade

log = logging.getLogger("engine.live")

_END = ("end", None)


def _sign(x: int) -> int:
    return int(x > 0) - int(x < 0)


class LiveEngine:
    def __init__(self, feed, broker, strategies, clock, blotter: Blotter,
                 reconnect_delay: float = 1.0, drain_timeout: float = 0.5,
                 warmup_gate: bool = True) -> None:
        self.feed = feed
        self.broker = broker
        self.strategies = list(strategies)
        self.clock = clock
        self.blotter = blotter
        self.reconnect_delay = reconnect_delay
        self.drain_timeout = drain_timeout
        # SAFETY: on connect the feed replays historical backfill bars (bars only,
        # no trades) so bar-driven features warm up. Those bars would otherwise
        # make strategies emit HISTORICAL orders that fill at the LIVE price. While
        # gated, we still dispatch events (state warms) but DROP the orders. The
        # first live Trade (backfill has none) flips us live. warmup_gate=False for
        # feeds with no backfill (tests).
        self.warmup_gate = warmup_gate
        self._live = not warmup_gate
        self._backfill_bars = 0
        self._suppressed_orders = 0
        # ghost signals: (ts, side, qty, tag, px) the strategies WOULD have sent
        # during warmup — consumed by the chart painter after the live flip
        self.warmup_signals: list[tuple[int, int, int, str, float]] = []
        self.last_px: float = 0.0
        # optional async callbacks for the chart painter
        self.on_live_order = None          # async (ts, side, qty, tag, px)
        self.on_bar_hook = None            # async (ts, close, live, backfill_bars)
        self.on_warmup_signal = None       # async (ts, side, qty, tag, px) — ghosts
        # ── per-strategy attribution ──────────────────────────────────────
        # Multiple sleeves share one ACCOUNT, so the account net position must
        # never be broadcast to strategies (each sleeve would mis-attribute the
        # others' inventory to itself — the 2026-07-07 runaway). Instead: every
        # order is owned by the strategy that emitted it; fills are routed ONLY
        # to their owner, which also receives a synthetic PositionUpdate of ITS
        # OWN book. Fills with unknown order_ids (manual trades) hit the blotter
        # only. reduce_only is enforced HERE against the owner's attributed
        # position (the NT path cannot enforce it).
        self._owner: dict[str, object] = {}         # order_id -> strategy
        self._spos: dict[int, int] = {}              # id(strategy) -> signed qty
        self._savg: dict[int, float] = {}            # id(strategy) -> avg px
        self._q: asyncio.Queue = asyncio.Queue()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def strategy_position(self, s) -> int:
        return self._spos.get(id(s), 0)

    def _vet(self, s, o):
        """Enforce reduce_only against the OWNER's attributed position."""
        if not o.reduce_only:
            return o
        pos = self._spos.get(id(s), 0)
        if pos == 0 or _sign(o.side) == _sign(pos):
            log.info("dropped reduce_only %s from %s (own pos %d)",
                     o.tag, type(s).__name__, pos)
            return None
        if o.qty > abs(pos):
            import dataclasses
            o = dataclasses.replace(o, qty=abs(pos))
        return o

    def _attribute_fill(self, f: Fill) -> None:
        s = self._owner.get(f.order_id)
        if s is None:
            log.info("unattributed fill (manual/external): %s", f)
            return
        pid = id(s)
        old = self._spos.get(pid, 0)
        new = old + f.size
        if old == 0 or _sign(new) != _sign(old):
            self._savg[pid] = f.price               # fresh / flipped book
        elif _sign(f.size) == _sign(old):           # adding: weighted average
            self._savg[pid] = (self._savg[pid] * abs(old) + f.price * abs(f.size)) \
                / (abs(old) + abs(f.size))
        self._spos[pid] = new
        dispatch_broker([s], f)
        dispatch_broker([s], PositionUpdate(f.ts, f.symbol, new,
                                            self._savg.get(pid, f.price)))

    async def _pump_feed(self) -> None:
        while not self._stop.is_set():
            try:
                async for e in self.feed.stream():
                    await self._q.put(("market", e))
                await self._q.put(_END)          # stream ended cleanly
                return
            except asyncio.CancelledError:
                raise
            except Exception as ex:              # noqa: BLE001 - resilient live loop
                log.warning("feed error: %s; reconnecting in %.1fs", ex, self.reconnect_delay)
                await asyncio.sleep(self.reconnect_delay)

    async def _pump_broker(self) -> None:
        while not self._stop.is_set():
            try:
                async for be in self.broker.events():
                    await self._q.put(("broker", be))
                return
            except asyncio.CancelledError:
                raise
            except Exception as ex:              # noqa: BLE001
                log.warning("broker error: %s; reconnecting in %.1fs", ex, self.reconnect_delay)
                await asyncio.sleep(self.reconnect_delay)

    async def run(self) -> Blotter:
        feeder = asyncio.create_task(self._pump_feed())
        brokerer = asyncio.create_task(self._pump_broker())
        feed_done = False
        try:
            while not self._stop.is_set():
                try:
                    kind, ev = await asyncio.wait_for(self._q.get(), timeout=self.drain_timeout)
                except asyncio.TimeoutError:
                    if feed_done:                    # no events for a full drain window -> done
                        break
                    continue
                if kind == "end":
                    feed_done = True
                    continue
                if kind == "market":
                    self.clock.set(ev.ts)
                    if isinstance(ev, Trade):
                        self.last_px = ev.price
                    elif isinstance(ev, Bar):
                        self.last_px = ev.c
                    if not self._live and isinstance(ev, Trade):
                        self._live = True
                        for s in self.strategies:      # clear warmup phantom state
                            reset = getattr(s, "reset_for_live", None)
                            if callable(reset):
                                reset()
                        log.info("warmup complete: %d backfill bars consumed, %d "
                                 "warmup orders suppressed; strategies reset; now LIVE",
                                 self._backfill_bars, self._suppressed_orders)
                    if not self._live and isinstance(ev, Bar):
                        self._backfill_bars += 1
                    for s in self.strategies:            # per-strategy: orders are OWNED
                        for o in dispatch_market([s], ev):
                            if self._live:
                                o = self._vet(s, o)
                                if o is None:
                                    continue
                                self._owner[o.order_id] = s
                                self.blotter.on_order(o)
                                await self.broker.submit(o)
                                if self.on_live_order is not None:
                                    await self.on_live_order(ev.ts, o.side, o.qty,
                                                             o.tag, self.last_px)
                            else:                        # warmup: state only, no orders
                                self._suppressed_orders += 1
                                sig = (ev.ts, o.side, o.qty, o.tag, self.last_px)
                                self.warmup_signals.append(sig)
                                if self.on_warmup_signal is not None:
                                    await self.on_warmup_signal(*sig)   # ghost now
                    if isinstance(ev, Bar) and self.on_bar_hook is not None:
                        await self.on_bar_hook(ev, self._live, self._backfill_bars)
                    self.blotter.on_market_event(ev)
                else:  # broker event
                    self.blotter.on_broker_event(ev)
                    if isinstance(ev, Fill):
                        self._attribute_fill(ev)         # owner-only routing
                    # account-level PositionUpdates are NOT broadcast to
                    # strategies: each sleeve sees only its own attributed book
        finally:
            self._stop.set()
            for t in (feeder, brokerer):
                t.cancel()
            await asyncio.gather(feeder, brokerer, return_exceptions=True)
        return self.blotter


__all__ = ["LiveEngine"]
