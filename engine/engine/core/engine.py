"""The engine loop.

ReplayEngine drives a FeedAdapter deterministically (single async task,
event-time clock). The SAME Strategy objects are reused live — only the wiring
(Feed/Broker) changes. The loop:

    pull MarketEvent -> clock.set(ts) -> let broker match resting orders against
    the new price -> feed any resulting fills back to strategies -> dispatch the
    event to each strategy -> submit returned orders -> feed new fills back ->
    log everything.

The broker is expected to expose the sync replay hooks `on_market_event(e)` and
`drain() -> list[BrokerEvent]` (SimBroker does). A live engine variant (Phase 2)
will instead consume `broker.events()` concurrently with `feed.stream()`.
"""
from __future__ import annotations

import logging

from .blotter import Blotter
from .events import Bar, BookFlow, DepthUpdate, Fill, PositionUpdate, Quote, Trade
from .orders import Order

log = logging.getLogger("engine.loop")


class ReplayEngine:
    def __init__(self, feed, broker, strategies, clock, blotter: Blotter) -> None:
        self.feed = feed
        self.broker = broker
        self.strategies = list(strategies)
        self.clock = clock
        self.blotter = blotter

    async def run(self) -> Blotter:
        async for e in self.feed.stream():
            self.clock.set(e.ts)
            # 1. let the broker fill resting limit/stop orders against the new price
            self.broker.on_market_event(e)
            self._drain()
            # 2. dispatch the market event to each strategy
            orders = self._dispatch(e)
            # 3. submit returned orders (market orders may fill immediately)
            for o in orders:
                self.blotter.on_order(o)
                await self.broker.submit(o)
            self._drain()
            self.blotter.on_market_event(e)
        # end of stream: cancel resting orders, mark final state
        self.broker.on_end()
        self._drain()
        return self.blotter

    # ── dispatch helpers ─────────────────────────────────────────────────
    def _dispatch(self, e) -> list[Order]:
        out: list[Order] = []
        for s in self.strategies:
            if isinstance(e, Trade):
                out += s.on_trade(e) or []
            elif isinstance(e, BookFlow):
                out += s.on_bookflow(e) or []
            elif isinstance(e, Bar):
                out += s.on_bar(e) or []
            elif isinstance(e, Quote):
                out += s.on_quote(e) or []
            elif isinstance(e, DepthUpdate):
                out += s.on_depth(e) or []
        return out

    def _drain(self) -> None:
        for be in self.broker.drain():
            self.blotter.on_broker_event(be)
            for s in self.strategies:
                if isinstance(be, Fill):
                    s.on_fill(be)
                elif isinstance(be, PositionUpdate):
                    s.on_position(be)


__all__ = ["ReplayEngine"]
