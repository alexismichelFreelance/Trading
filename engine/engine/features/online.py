"""Incremental, strictly-causal per-second feature engines.

These run identically in replay and live. The unit is the 1-second flow bucket,
closed by a BookFlow event (the end-of-second marker the ReplayFeed emits after
that second's reconstructed trades; live, the feature layer emits it after
aggregating raw trades + DOM for the second).

IgnitionFeatures reproduces the precomputed `claude_sec_feat` columns EXACTLY
(verified bit-for-bit in tests/test_features_online.py — Gate A):
    adelta = sum signed trade size in the second   (buy +, sell -)
    avol   = sum |trade size| in the second
    str    = |adelta| / (mean(previous up-to-120 within-day |adelta|) + 1)
             -> first row of each ET day is None (no prior baseline)
    dir    = sign(adelta), carried through adelta==0 quiet seconds
    pxc    = the second's representative trade price (carried when no trades)
"""
from __future__ import annotations

from collections import deque

from ..core.events import BookFlow, Trade
from ..core.timeutil import et_session_date

IGNITION_BASELINE = 120


class _RollingMean:
    """O(1) rolling mean over the last `maxlen` pushed values."""

    __slots__ = ("dq", "maxlen", "sum")

    def __init__(self, maxlen: int) -> None:
        self.dq: deque[float] = deque()
        self.maxlen = maxlen
        self.sum = 0.0

    def mean(self) -> float | None:
        return self.sum / len(self.dq) if self.dq else None

    def push(self, x: float) -> None:
        self.dq.append(x)
        self.sum += x
        if len(self.dq) > self.maxlen:
            self.sum -= self.dq.popleft()

    def clear(self) -> None:
        self.dq.clear()
        self.sum = 0.0


class IgnitionFeatures:
    """Per-second aggressor-flow + resting-book features for the ignition signal."""

    def __init__(self, baseline: int = IGNITION_BASELINE, trend_lag: int = 300) -> None:
        self._roll = _RollingMean(baseline)
        self._day: str | None = None
        # current-second accumulators
        self._adelta = 0
        self._avol = 0
        self._have_trade = False
        # finalized per-second features (read by the strategy in on_bookflow)
        self.ts: int | None = None
        self.adelta: int | None = None
        self.avol: int | None = None
        self.strength: float | None = None     # the `str` column (str is a builtin)
        self.dir: int = 0
        self.pxc: float | None = None
        self.bid_cancel = self.ask_cancel = self.bid_add = self.ask_add = 0
        # trend-alignment price lag (~prior 300 seconds on the ~1Hz grid)
        self._pxc_lag: deque[float] = deque(maxlen=trend_lag + 1)

    # ── inputs ───────────────────────────────────────────────────────────
    def add_trade(self, t: Trade) -> None:
        self._adelta += t.aggressor * t.size
        self._avol += t.size
        self.pxc = t.price
        self._have_trade = True

    def close_second(self, bf: BookFlow) -> None:
        day = et_session_date(bf.ts)
        if day != self._day:
            self._day = day
            self._roll.clear()
        ad, av = self._adelta, self._avol
        absd = abs(ad)
        m = self._roll.mean()
        self.strength = None if m is None else absd / (m + 1.0)
        self._roll.push(float(absd))
        if ad != 0:
            self.dir = 1 if ad > 0 else -1
        self.ts = bf.ts
        self.adelta, self.avol = ad, av
        self.bid_cancel, self.ask_cancel = bf.bid_cancel, bf.ask_cancel
        self.bid_add, self.ask_add = bf.bid_add, bf.ask_add
        if self.pxc is not None:
            self._pxc_lag.append(self.pxc)
        # reset accumulators for the next second
        self._adelta = 0
        self._avol = 0
        self._have_trade = False

    # ── derived reads ────────────────────────────────────────────────────
    def trend_sign(self) -> int:
        """sign(pxc[now] - pxc[~300s ago]); 0 until enough history."""
        if self.pxc is None or len(self._pxc_lag) < self._pxc_lag.maxlen:
            return 0
        prior = self._pxc_lag[0]
        d = self.pxc - prior
        return 1 if d > 0 else (-1 if d < 0 else 0)

    def book_confirm(self, direction: int) -> bool:
        """Attacked side's resting book collapsing: up-ignition needs
        ask_cancel>bid_cancel, down needs bid_cancel>ask_cancel."""
        if direction > 0:
            return self.ask_cancel > self.bid_cancel
        if direction < 0:
            return self.bid_cancel > self.ask_cancel
        return False


__all__ = ["IgnitionFeatures", "IGNITION_BASELINE"]
