"""Daily floor pivots from the prior session H/L/C + prior-day H/L + round
numbers. The ignition strategy uses these as the trend-mode target FALLBACK
(when no live virgin zone) and for the opposite-side stop.

Floor pivots (classic):
  PP=(H+L+C)/3; R1=2PP-L; S1=2PP-H; R2=PP+(H-L); S2=PP-(H-L);
  R3=H+2(PP-L); S3=L-2(H-PP); plus PDH=H, PDL=L, and 50-pt round numbers.
"""
from __future__ import annotations

import math

import numpy as np

from ..core.timeutil import et, et_session_date, is_rth


def daily_pivots(prior_high: float, prior_low: float, prior_close: float) -> dict[str, float]:
    pp = (prior_high + prior_low + prior_close) / 3.0
    rng = prior_high - prior_low
    return {
        "PP": pp, "R1": 2 * pp - prior_low, "S1": 2 * pp - prior_high,
        "R2": pp + rng, "S2": pp - rng,
        "R3": prior_high + 2 * (pp - prior_low), "S3": prior_low - 2 * (prior_high - pp),
        "PDH": prior_high, "PDL": prior_low,
    }


def level_set(prior_high: float, prior_low: float, prior_close: float,
              round_step: float = 50.0, pad: float = 60.0) -> list[float]:
    piv = list(daily_pivots(prior_high, prior_low, prior_close).values())
    lo = min(piv) - pad
    hi = max(piv) + pad
    rounds = list(np.arange(np.floor(lo / round_step) * round_step,
                            hi + round_step, round_step))
    return sorted(set(round(x, 4) for x in piv + rounds))


def nearest_beyond(levels: list[float], price: float, direction: int,
                   min_dist: float = 2.0) -> float | None:
    """Nearest level strictly beyond `price` in `direction`, >= min_dist away."""
    if direction > 0:
        cands = [x for x in levels if x >= price + min_dist]
        return min(cands) if cands else None
    cands = [x for x in levels if x <= price - min_dist]
    return max(cands) if cands else None


class SessionLevels:
    """Tracks RTH session H/L/C online; exposes prior-session floor pivots and
    targets/stops. The TARGET is the nearest of {prior-session pivots, the
    dynamically-computed nearest 50-pt round} beyond entry, so a target ALWAYS
    exists even when price has run far from the prior-day pivots (the research's
    50-round numbers are effectively infinite). The STOP is the nearest opposite
    PIVOT (structural; gives a trend trade room before the -12 cap)."""

    def __init__(self, round_step: float = 50.0) -> None:
        self.round_step = round_step
        self._day: str | None = None
        self._h = self._l = self._c = None
        self._prior: tuple[float, float, float] | None = None
        self.pivots: list[float] = []

    def update_bar(self, bar) -> None:
        if not is_rth(bar.ts):
            return
        day = et_session_date(bar.ts)
        if day != self._day:
            if self._h is not None:
                self._prior = (self._h, self._l, self._c)
            self._day = day
            self._h, self._l, self._c = bar.h, bar.l, bar.c
            self.pivots = (list(daily_pivots(*self._prior).values())
                           if self._prior is not None else [])
        else:
            self._h = max(self._h, bar.h)
            self._l = min(self._l, bar.l)
            self._c = bar.c

    def _round(self, price: float, direction: int, min_dist: float) -> float:
        step = self.round_step
        if direction > 0:
            return math.ceil((price + min_dist) / step) * step
        return math.floor((price - min_dist) / step) * step

    def target(self, price: float, direction: int, min_dist: float = 2.0) -> float:
        cands = [x for x in self.pivots
                 if (x >= price + min_dist if direction > 0 else x <= price - min_dist)]
        cands.append(self._round(price, direction, min_dist))
        return min(cands) if direction > 0 else max(cands)

    def stop(self, price: float, direction: int, min_dist: float = 2.0) -> float | None:
        return nearest_beyond(self.pivots, price, -direction, min_dist)


_PIVOT_NAMES = ("PP", "R1", "R2", "R3", "S1", "S2", "S3")


class MultiPivots:
    """Floor pivots across DAY + WEEK + MONTH from the bar stream — the full
    picture (daily alone is a small window). Tracks each period's H/L/C and
    exposes the merged PRIOR-period pivots, each labelled TF-name (e.g. 'D-S2',
    'W-PP', 'M-R1'). Prior periods are fixed intraday, so the grid is stable
    within a session and only shifts at a day/week/month boundary."""

    _TFS = ("D", "W", "M")

    def __init__(self) -> None:
        self._cur: dict[str, list] = {}      # tf -> [key, h, l, c]
        self.prior: dict[str, tuple] = {}    # tf -> (h, l, c) of last completed period

    @staticmethod
    def _key(tf: str, ts: int) -> str:
        if tf == "D":
            return et_session_date(ts)
        t = et(ts)
        if tf == "W":
            iso = t.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        return t.strftime("%Y-%m")

    def update(self, ts: int, h: float, l: float, c: float,
               rth_only: bool = True) -> None:
        # RTH ONLY. Classic floor pivots are defined on the prior RTH session --
        # this class's own docstring says so, and SessionLevels.update_bar has
        # always filtered. This one did not, so every overnight Globex bar was
        # folded into the D/W/M periods the sleeve trades. On 2026-08-07 that put
        # the daily low 18 points below the real RTH low, PP 7 points off and S1
        # 14 points off, and a pivot entry fired with price nowhere near a level.
        # A monthly period built from one overnight spike stays wrong for a month.
        # rth_only=False is for bars that are ALREADY whole-session aggregates
        # (the daily history seed): they carry a session's H/L/C and are stamped
        # at the session close, which is not itself an RTH minute, so filtering
        # them would reject the entire seed.
        if rth_only and not is_rth(ts):
            return
        for tf in self._TFS:
            key = self._key(tf, ts)
            cur = self._cur.get(tf)
            if cur is None or cur[0] != key:     # period rolled
                if cur is not None:
                    self.prior[tf] = (cur[1], cur[2], cur[3])
                self._cur[tf] = [key, h, l, c]
            else:
                cur[1] = max(cur[1], h)
                cur[2] = min(cur[2], l)
                cur[3] = c

    def grid(self) -> dict[float, str]:
        """{price: 'TF-name'} — the 7 floor pivots of each available prior period,
        merged (a shared price keeps the finest-timeframe label)."""
        out: dict[float, str] = {}
        for tf in self._TFS:                     # D first so W/M don't overwrite its label
            hlc = self.prior.get(tf)
            if hlc is None:
                continue
            for name, px in daily_pivots(*hlc).items():
                if name in _PIVOT_NAMES:
                    out.setdefault(round(px, 2), f"{tf}-{name}")
        return out


__all__ = ["daily_pivots", "level_set", "nearest_beyond", "SessionLevels",
           "MultiPivots"]
