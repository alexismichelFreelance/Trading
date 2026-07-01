"""FlowFollowingStrategy — event-driven, single continuous position.

The live/replay engine form of the flow oracle: per second, hold a position
proportional to a thresholded windowed aggressor-flow signal, with the
asymmetric add/hold band. Flat reset each session. The SAME decision logic as
flow_oracle.py (which is the parity gate); this version emits real orders and is
driven by the engine, so its P&L reflects realistic fills.
"""
from __future__ import annotations

from collections import deque

from ..core.events import BookFlow, Trade
from ..core.orders import Order
from ..core.timeutil import et_session_date
from .base import BaseStrategy


def _jsround(x: float) -> int:
    import math
    return math.floor(x + 0.5)


def _sign(x: float) -> int:
    return int(x > 0) - int(x < 0)


class FlowFollowingStrategy(BaseStrategy):
    def __init__(self, symbol: str, *, w: int = 120, th: int = 200, scale: float = 3000.0,
                 maxp: int = 50, add_band: int = 1, hold_band: int = 5) -> None:
        self.symbol = symbol
        self.w, self.th, self.scale, self.maxp = w, th, scale, maxp
        self.add_band, self.hold_band = add_band, hold_band
        self._buf: deque[float] = deque()
        self._F = 0.0
        self._day: str | None = None
        self._adelta = 0
        self.pos = 0

    def on_trade(self, t: Trade) -> list[Order]:
        self._adelta += t.aggressor * t.size
        return []

    def on_bookflow(self, bf: BookFlow) -> list[Order]:
        orders: list[Order] = []
        day = et_session_date(bf.ts)
        if day != self._day:                      # flat reset each session
            self._day = day
            self._buf.clear()
            self._F = 0.0
            if self.pos != 0:
                orders.append(Order(self.symbol, -_sign(self.pos), abs(self.pos),
                                    tag="session-flat", reduce_only=True))
            held = 0
        else:
            held = self.pos

        ad, self._adelta = self._adelta, 0
        x = ad if abs(ad) >= self.th else 0
        self._buf.append(x)
        self._F += x
        if len(self._buf) > self.w:
            self._F -= self._buf.popleft()

        tgt = max(-self.maxp, min(self.maxp, self._F / self.scale))
        delta = tgt - held
        band = self.add_band if held == 0 else (
            self.add_band if _sign(delta) == _sign(held) else self.hold_band)
        if abs(delta) > band:
            step = _jsround(tgt) - held
            if step != 0:
                orders.append(Order(self.symbol, 1 if step > 0 else -1, abs(step), tag="flow"))
        return orders

    def on_position(self, p) -> None:
        self.pos = p.qty


__all__ = ["FlowFollowingStrategy"]
