"""BaseStrategy — no-op defaults so concrete strategies override only what they
use. Satisfies the core.ports.Strategy protocol structurally."""
from __future__ import annotations

from ..core.events import Bar, BookFlow, DepthUpdate, Fill, PositionUpdate, Quote, Trade
from ..core.orders import Order


class BaseStrategy:
    symbol: str = ""

    def on_trade(self, e: Trade) -> list[Order]:
        return []

    def on_quote(self, e: Quote) -> list[Order]:
        return []

    def on_depth(self, e: DepthUpdate) -> list[Order]:
        return []

    def on_bar(self, e: Bar) -> list[Order]:
        return []

    def on_bookflow(self, e: BookFlow) -> list[Order]:
        return []

    def on_fill(self, e: Fill) -> None:
        return None

    def on_position(self, e: PositionUpdate) -> None:
        return None


__all__ = ["BaseStrategy"]
