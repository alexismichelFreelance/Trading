"""Ports — the interfaces between the pure core and the pluggable adapters.

These are typing.Protocols (structural): an adapter satisfies a port by shape,
it need not inherit anything. The engine depends only on these, never on a
concrete feed or broker. The SAME Strategy object is driven in replay and live.
"""
from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from .events import (
    Bar,
    BookFlow,
    BrokerEvent,
    DepthUpdate,
    Fill,
    MarketEvent,
    PositionUpdate,
    Quote,
    Trade,
)
from .orders import Order


@runtime_checkable
class Clock(Protocol):
    def now(self) -> int: ...
    def set(self, ts: int) -> None: ...


@runtime_checkable
class FeedAdapter(Protocol):
    def stream(self) -> AsyncIterator[MarketEvent]:
        """Async-yield normalized market events in STRICT ascending ts order."""
        ...


@runtime_checkable
class BrokerAdapter(Protocol):
    async def submit(self, order: Order) -> None: ...
    async def cancel(self, order_id: str) -> None: ...
    async def modify(self, order_id: str, **changes) -> None: ...
    def events(self) -> AsyncIterator[BrokerEvent]:
        """Async-yield Fill / PositionUpdate / AccountUpdate as they occur."""
        ...


@runtime_checkable
class Strategy(Protocol):
    """Stateful, strictly causal. Handlers return orders to send; inbound
    broker events update internal state. Holds its own incremental feature
    engines — no look-ahead, no indexing into the future."""
    symbol: str

    def on_trade(self, e: Trade) -> list[Order]: ...
    def on_quote(self, e: Quote) -> list[Order]: ...
    def on_depth(self, e: DepthUpdate) -> list[Order]: ...
    def on_bar(self, e: Bar) -> list[Order]: ...
    def on_bookflow(self, e: BookFlow) -> list[Order]: ...
    def on_fill(self, e: Fill) -> None: ...
    def on_position(self, e: PositionUpdate) -> None: ...


__all__ = ["Clock", "FeedAdapter", "BrokerAdapter", "Strategy"]
