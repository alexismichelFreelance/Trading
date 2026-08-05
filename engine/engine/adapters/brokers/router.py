"""SymbolRouterBroker — one BrokerAdapter facade over {symbol: broker}.

Keeps single-symbol brokers (SimBroker etc.) untouched: each stays a
single-book instance; this router directs orders by `Order.symbol`, market
events by `event.symbol` (broadcast when unstamped), and fans broker events
back into one stream. Composition partner of MergeFeed for multi-instrument
replay; also usable live over N socket brokers.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from ...core.events import BrokerEvent

log = logging.getLogger("engine.router")


class SymbolRouterBroker:
    def __init__(self, brokers: dict[str, object]) -> None:
        if not brokers:
            raise ValueError("SymbolRouterBroker needs at least one broker")
        self.brokers = dict(brokers)
        self._route: dict[str, object] = {}   # order_id -> broker (cancel/modify)

    def _for_symbol(self, symbol: str):
        try:
            return self.brokers[symbol]
        except KeyError:
            raise KeyError(f"no broker for symbol {symbol!r} "
                           f"(have {sorted(self.brokers)})") from None

    # ── market side ─────────────────────────────────────────────────────────
    def on_market_event(self, e) -> None:
        sym = getattr(e, "symbol", "")
        if sym and sym in self.brokers:
            self.brokers[sym].on_market_event(e)
        else:                                  # unstamped: broadcast (legacy)
            for b in self.brokers.values():
                b.on_market_event(e)

    # ── order side ──────────────────────────────────────────────────────────
    async def submit(self, order) -> None:
        b = self._for_symbol(order.symbol)
        self._route[order.order_id] = b
        await b.submit(order)

    async def cancel(self, order_id: str) -> None:
        b = self._route.get(order_id)
        if b is not None:
            await b.cancel(order_id)

    async def modify(self, order_id: str, **changes) -> None:
        b = self._route.get(order_id)
        if b is not None:
            await b.modify(order_id, **changes)

    # ── broker events ───────────────────────────────────────────────────────
    def drain(self) -> list[BrokerEvent]:
        """Sync drain (replay path) — concatenated in dict insertion order."""
        out: list[BrokerEvent] = []
        for b in self.brokers.values():
            if hasattr(b, "drain"):
                out += b.drain()
        return out

    def on_end(self) -> None:
        for b in self.brokers.values():
            if hasattr(b, "on_end"):
                b.on_end()

    async def events(self) -> AsyncIterator[BrokerEvent]:
        """Async fan-in (live path) — merges every broker's events stream.

        The FIRST leg to end ends the fan-in. A broker stream ending is a
        dropped connection, and LiveEngine only reconnects when events()
        returns: draining the survivors instead would leave that instrument
        with no fills for the rest of the session while the engine saw a
        perfectly healthy broker. Same rule as MergeFeed.
        """
        q: asyncio.Queue = asyncio.Queue()
        DONE = object()

        async def pump(sym, b):
            try:
                async for ev in b.events():
                    await q.put(ev)
            finally:
                await q.put((DONE, sym))

        tasks = [asyncio.create_task(pump(s, b)) for s, b in self.brokers.items()]
        try:
            while True:
                ev = await q.get()
                if isinstance(ev, tuple) and ev and ev[0] is DONE:
                    log.warning("BROKER leg %s ended: treating as a DISCONNECT "
                                "and ending the fan-in so every leg reconnects",
                                ev[1])
                    return
                yield ev
        finally:
            for t in tasks:
                t.cancel()


__all__ = ["SymbolRouterBroker"]
