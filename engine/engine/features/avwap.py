"""Anchored VWAP — volume-weighted average price from a chosen anchor bar to
now. Unlike session VWAP (which resets every day), the anchor is picked to mark
a shift in participation (session open, a swing, a news bar, week/month start),
so the line reads the average COST BASIS of everyone who transacted since that
event: above it that cohort is in profit (line tends to support), below it they
are underwater (rallies to it get sold — resistance).

`AnchoredVWAP` maintains the running sums incrementally (three floats), so any
number of anchors cost O(1) per bar. `MultiAVWAP` drives the periodic anchors
(RTH-open / week-to-date / month-to-date) off a bar's session/week/month key,
resetting each at its own boundary. Structural anchors (prior-day high/low,
swing high/low, event bars) are anchored by index/time by the caller.

The offline study (tools/avwap_study.py) cross-checks this math against a
prefix-sum computation; see tests/test_avwap.py.
"""
from __future__ import annotations

import math


class AnchoredVWAP:
    """One anchored VWAP: feed (price, volume) in time order; read `value` and
    `sigma` (the volume-weighted dispersion, for +/- band construction)."""

    __slots__ = ("cum_pv", "cum_v", "cum_ppv", "n", "anchor_ts")

    def __init__(self) -> None:
        self.reset(0)

    def reset(self, anchor_ts: int = 0) -> None:
        self.cum_pv = 0.0        # Σ p·v
        self.cum_v = 0.0         # Σ v
        self.cum_ppv = 0.0       # Σ p²·v  (for variance/bands)
        self.n = 0
        self.anchor_ts = anchor_ts

    def add(self, px: float, vol: float) -> None:
        v = float(vol)
        if v <= 0.0:
            return               # zero/again-negative volume contributes nothing
        self.cum_pv += px * v
        self.cum_v += v
        self.cum_ppv += px * px * v
        self.n += 1

    @property
    def value(self) -> float:
        return self.cum_pv / self.cum_v if self.cum_v > 0.0 else math.nan

    @property
    def sigma(self) -> float:
        if self.cum_v <= 0.0:
            return 0.0
        m = self.cum_pv / self.cum_v
        var = self.cum_ppv / self.cum_v - m * m
        return math.sqrt(var) if var > 0.0 else 0.0

    def band(self, k: float) -> tuple[float, float]:
        """(lower, upper) at ±k·sigma around the line."""
        m, s = self.value, self.sigma
        return (m - k * s, m + k * s)


class MultiAVWAP:
    """The periodic anchors, maintained together. Each named anchor resets when
    its period key changes:
        rth_open -> session date (daily reset at the RTH open)
        wtd      -> ISO year-week (resets Monday)
        mtd      -> year-month   (resets on the 1st)
    Feed every bar via `update`; read `values()` / `value(name)` / `sigma(name)`.
    """

    _KEYFN = {
        "rth_open": lambda sd, wk, mo: sd,
        "wtd": lambda sd, wk, mo: wk,
        "mtd": lambda sd, wk, mo: mo,
    }

    def __init__(self, anchors: tuple[str, ...] = ("rth_open", "wtd", "mtd")) -> None:
        bad = [a for a in anchors if a not in self._KEYFN]
        if bad:
            raise ValueError(f"unknown periodic anchors: {bad}")
        self._names = tuple(anchors)
        self._av = {n: AnchoredVWAP() for n in anchors}
        self._key: dict[str, object] = {}

    def update(self, ts: int, px: float, vol: float,
               sess_date: str, iso_week: str, month: str) -> None:
        for n in self._names:
            k = self._KEYFN[n](sess_date, iso_week, month)
            if self._key.get(n) != k:
                self._av[n].reset(ts)
                self._key[n] = k
            self._av[n].add(px, vol)

    def value(self, name: str) -> float:
        return self._av[name].value

    def sigma(self, name: str) -> float:
        return self._av[name].sigma

    def values(self) -> dict[str, float]:
        return {n: self._av[n].value for n in self._names}


__all__ = ["AnchoredVWAP", "MultiAVWAP"]
