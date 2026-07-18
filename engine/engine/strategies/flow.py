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
from ..core.timeutil import et_session_date, ns_to_utc
from .base import BaseStrategy


def _jsround(x: float) -> int:
    import math
    return math.floor(x + 0.5)


def _sign(x: float) -> int:
    return int(x > 0) - int(x < 0)


class FlowFollowingStrategy(BaseStrategy):
    def __init__(self, symbol: str, *, w: int = 120, th: int = 200, scale: float = 3000.0,
                 maxp: int = 50, add_band: int = 1, hold_band: int = 5,
                 gate_utc: tuple[int, int] | None = (13, 21),
                 adaptive: bool = False, adapt_k: float = 4.0,
                 vol_win: int = 1800, warm: int = 300, gamma=None) -> None:
        self.symbol = symbol
        # optional GammaRegime: position INCREASES only on short-gamma days
        # (reduces toward flat always pass) — a strategy choice, not an engine gate
        self.gamma = gamma
        self.w, self.th, self.scale, self.maxp = w, th, scale, maxp
        self.add_band, self.hold_band = add_band, hold_band
        # ADAPTIVE (scale-invariant) threshold: th_t = rolling_mean + k*rolling_std
        # of |adelta| over a trailing vol_win seconds (reset per day, `warm`-sec
        # warmup); scale_t keeps the validated th:scale = 1:15 ratio. This makes
        # flow fire on ANY feed's own distribution (the live NT feed's |adelta|
        # tops ~66, so fixed th=200 never fires). NOTE: adaptive is NOT more
        # robust than fixed on the research data — flow is the fragile sleeve
        # either way (strategy_lab/FLOW_ADAPTIVE_STUDY.md). Default OFF (parity).
        self.adaptive, self.adapt_k = adaptive, adapt_k
        self.vol_win, self.warm = vol_win, warm
        self.gate_utc = gate_utc
        self._buf: deque[float] = deque()
        self._F = 0.0
        self._day: str | None = None
        self._adelta = 0
        self.pos = 0
        self._flattening = 0           # >0: flatten in flight (countdown to retry)
        self._vbuf: deque[float] = deque()   # trailing |adelta| for the vol estimate
        self._vsum = 0.0
        self._vsq = 0.0

    def _adapt(self, absad: float) -> tuple[float, float]:
        """Update the trailing |adelta| stats and return (th_t, scale_t)."""
        self._vbuf.append(absad)
        self._vsum += absad
        self._vsq += absad * absad
        if len(self._vbuf) > self.vol_win:
            old = self._vbuf.popleft()
            self._vsum -= old
            self._vsq -= old * old
        n = len(self._vbuf)
        if n < self.warm:
            return float("inf"), 1.0          # not warm: emit nothing
        mean = self._vsum / n
        var = max(0.0, self._vsq / n - mean * mean)
        th = mean + self.adapt_k * (var ** 0.5)
        if th <= 0.0:                          # dead-quiet window (all-zero flow):
            return float("inf"), 1.0           # nothing to measure -> emit nothing
        return th, 15.0 * th

    def on_trade(self, t: Trade) -> list[Order]:
        self._adelta += t.aggressor * t.size
        return []

    def on_bookflow(self, bf: BookFlow) -> list[Order]:
        orders: list[Order] = []
        if self.gate_utc is not None:
            h = ns_to_utc(bf.ts).hour
            if not (self.gate_utc[0] <= h < self.gate_utc[1]):
                self._buf.clear()
                self._F = 0.0
                self._adelta = 0
                if self.pos != 0:
                    if self._flattening > 0:      # flatten in flight: wait
                        self._flattening -= 1     # (bounded retry, ~30s cadence)
                        return []
                    self._flattening = 30
                    return [Order(self.symbol, -_sign(self.pos), abs(self.pos),
                                  tag="window-flat", reduce_only=True)]
                return []
        day = et_session_date(bf.ts)
        if day != self._day:                      # flat reset each session
            self._day = day
            self._buf.clear()
            self._F = 0.0
            self._vbuf.clear()                    # re-warm the vol estimate daily
            self._vsum = self._vsq = 0.0
            if self.pos != 0:
                orders.append(Order(self.symbol, -_sign(self.pos), abs(self.pos),
                                    tag="session-flat", reduce_only=True))
            held = 0
        else:
            held = self.pos

        ad, self._adelta = self._adelta, 0
        th, scale = self._adapt(abs(ad)) if self.adaptive else (self.th, self.scale)
        x = ad if abs(ad) >= th else 0
        self._buf.append(x)
        self._F += x
        if len(self._buf) > self.w:
            self._F -= self._buf.popleft()

        if scale <= 0:                            # defensive: never divide by zero
            return orders
        tgt = max(-self.maxp, min(self.maxp, self._F / scale))
        delta = tgt - held
        band = self.add_band if held == 0 else (
            self.add_band if _sign(delta) == _sign(held) else self.hold_band)
        if abs(delta) > band:
            step = _jsround(tgt) - held
            if step != 0:
                if abs(held + step) > abs(held) and not self.gamma_entry_ok(bf.ts, "short"):
                    return orders            # block INCREASES off-regime; reduces pass
                orders.append(Order(self.symbol, 1 if step > 0 else -1, abs(step), tag="flow"))
        return orders

    def on_position(self, p) -> None:
        self.pos = p.qty
        if p.qty == 0:
            self._flattening = 0


__all__ = ["FlowFollowingStrategy"]
