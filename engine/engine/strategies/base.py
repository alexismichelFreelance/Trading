"""BaseStrategy — no-op defaults so concrete strategies override only what they
use. Satisfies the core.ports.Strategy protocol structurally."""
from __future__ import annotations

from ..core.events import Bar, BookFlow, DepthUpdate, Fill, PositionUpdate, Quote, Trade
from ..core.orders import Order
from ..core.timeutil import et_session_date


class BaseStrategy:
    symbol: str = ""
    gamma = None          # optional GammaRegime — a STRATEGY choice, not an engine gate

    def gamma_entry_ok(self, ts: int, want: str) -> bool:
        """Dealer-gamma ENTRY filter, opt-in per strategy (set self.gamma).
        want='short': enter only on short-gamma days (trend sleeves earn there —
        gamma/GEX_FINDINGS.md D); want='long': only on mid/long-gamma days
        (mean-reversion). Exits are never filtered (call this only on entries).
        Fail-open: unknown regime (no GEX row) allows."""
        if self.gamma is None:
            return True
        sg = self.gamma.is_short_gamma(et_session_date(ts))
        if sg is None:
            return True
        return sg if want == "short" else not sg

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
