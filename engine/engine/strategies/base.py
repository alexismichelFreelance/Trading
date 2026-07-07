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

    def reset_for_live(self) -> None:
        """Called ONCE at the warmup->live flip. Warmup dispatches bars so
        feature/zone detectors warm up, but the warmup gate SUPPRESSES the
        resulting orders — leaving any 'I've acted' trade-lifecycle state
        (self.trade, self.entered, fade_done, position) corrupted by trades that
        never actually filled. Override to reset that trade state to flat/fresh
        while KEEPING warm detection (zones, averages, HMM). No-op by default;
        never called in replay, so parity is unaffected."""
        return None


__all__ = ["BaseStrategy"]
