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

log = logging.getLogger("engine.live")

_END = ("end", None)


class LiveEngine:
    def __init__(self, feed, broker, strategies, clock, blotter: Blotter,
                 reconnect_delay: float = 1.0, drain_timeout: float = 0.5) -> None:
        self.feed = feed
        self.broker = broker
        self.strategies = list(strategies)
        self.clock = clock
        self.blotter = blotter
        self.reconnect_delay = reconnect_delay
        self.drain_timeout = drain_timeout
        self._q: asyncio.Queue = asyncio.Queue()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

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
                    for o in dispatch_market(self.strategies, ev):
                        self.blotter.on_order(o)
                        await self.broker.submit(o)
                    self.blotter.on_market_event(ev)
                else:  # broker event
                    self.blotter.on_broker_event(ev)
                    dispatch_broker(self.strategies, ev)
        finally:
            self._stop.set()
            for t in (feeder, brokerer):
                t.cancel()
            await asyncio.gather(feeder, brokerer, return_exceptions=True)
        return self.blotter


__all__ = ["LiveEngine"]
