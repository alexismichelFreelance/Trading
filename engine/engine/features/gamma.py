"""GammaRegime — daily dealer-gamma regime from the claude_gex table
(SqueezeMetrics aggregate GEX; loaded by tools/fetch_gex.py).

CAUSAL: the regime for trading day D is the PRIOR session's EOD gexp (the
trailing-252-session percentile of aggregate GEX). Low percentile = dealers
short gamma = moves amplified (trend sleeves on); high = dealers long gamma =
pinning/mean-reversion (structure sleeves favored).

Validated on Feb-May 2025 (70 sessions): rank(gexp_prev, next-day RTH range)
= -0.63 (-0.43 partial after vol persistence); trend-sleeve $ concentrates
almost entirely in gexp_prev <= 1/3 days (gate keeps 96% of portfolio P&L at
-27% max drawdown; inverse rule collapses to ~1/8th). Direction is NOT
predicted (rank -0.09 vs efficiency) — this is a SIZE/regime signal only.
"""
from __future__ import annotations

import logging
from datetime import date

from ..adapters.questdb import QuestDB

log = logging.getLogger("engine.gamma")

TREND_MAX_PCTL = 1.0 / 3.0     # gexp_prev at/below this -> short-gamma regime
# The regime is defined as the PRIOR SESSION's, so one business day. Fri->Mon is
# 3 calendar days and a long weekend is 4, so 4 is the loosest bound that still
# means "yesterday". Anything older is not this day's regime.
MAX_AGE_DAYS = 4


class GammaRegime:
    def __init__(self, q: QuestDB | None = None, table: str = "claude_gex") -> None:
        self._q = q or QuestDB()
        self._table = table
        self._stale_warned: str | None = None      # log once per day, not per bar
        self._snap: tuple[list, list] = ([], [])   # (dates, vals), swapped atomically
        self._load()

    def _load(self) -> None:
        df = self._q.df(f"SELECT ts, gexp FROM {self._table} "
                        f"WHERE gexp IS NOT NULL ORDER BY ts")
        dates = df["ts"].dt.strftime("%Y-%m-%d").tolist()
        vals = df["gexp"].astype(float).tolist()
        # single atomic rebind: a concurrent reader (a *_gex strategy in the
        # engine loop, while reload() runs in a worker thread) never sees a
        # half-updated dates/vals pair.
        self._snap = (dates, vals)

    def reload(self) -> None:
        """Re-read the table IN PLACE so long-running holders (the *_gex
        strategies keep one shared instance) pick up sessions written since
        construction — without this, gexp_prev freezes at the startup snapshot
        and drifts one session staler per day the engine runs. May raise on a
        slow/unreachable DB — the caller retries."""
        self._load()

    def gexp_prev(self, day: str, max_age_days: int = MAX_AGE_DAYS) -> float | None:
        """CAUSAL prior-session GEX percentile for trading day 'YYYY-MM-DD':
        the gexp of the latest session STRICTLY BEFORE `day`.

        Returns None if that session is more than `max_age_days` old. Without
        that bound this returned the newest row it had FOREVER, with no signal
        that it was stale: on 2026-07-29 the scheduled fetch had been dying at
        its 15-minute Windows time limit since 07-22, and every *_gex sleeve was
        gating on 07-23 gamma for six sessions while reporting nothing wrong.
        Stale regime data is worse than none -- it is a confident wrong answer --
        so it degrades to None, which gamma_entry_ok fails OPEN on, making a
        *_gex sleeve behave as its ungated twin instead of on fiction."""
        import bisect
        dates, vals = self._snap          # consistent snapshot even mid-reload
        i = bisect.bisect_left(dates, day)
        if i == 0:
            return None
        prev = dates[i - 1]
        age = (date.fromisoformat(day) - date.fromisoformat(prev)).days
        if age > max_age_days:
            if self._stale_warned != day:
                self._stale_warned = day
                log.error("GAMMA DATA STALE: newest session before %s is %s "
                          "(%d days old, limit %d). %s is not being updated -- "
                          "check the Trading_GEX_Daily task and gamma/fetch_log.txt. "
                          "Gating DISABLED until it is fresh.",
                          day, prev, age, max_age_days, self._table)
            return None
        return vals[i - 1]

    def age_days(self, day: str) -> int | None:
        """Age in days of the newest session strictly before `day` — for health
        reporting, so staleness is visible without waiting for a sleeve to trip."""
        import bisect
        dates, _ = self._snap
        i = bisect.bisect_left(dates, day)
        if i == 0:
            return None
        return (date.fromisoformat(day) - date.fromisoformat(dates[i - 1])).days

    def is_short_gamma(self, day: str, max_pctl: float = TREND_MAX_PCTL) -> bool | None:
        """True = short-gamma regime (trend sleeves on). None = no data OR the
        data is too old to describe `day`."""
        v = self.gexp_prev(day)
        return None if v is None else v <= max_pctl


__all__ = ["GammaRegime", "TREND_MAX_PCTL", "MAX_AGE_DAYS"]
