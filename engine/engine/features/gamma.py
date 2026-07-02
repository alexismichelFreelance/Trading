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

from ..adapters.questdb import QuestDB

TREND_MAX_PCTL = 1.0 / 3.0     # gexp_prev at/below this -> short-gamma regime


class GammaRegime:
    def __init__(self, q: QuestDB | None = None, table: str = "claude_gex") -> None:
        q = q or QuestDB()
        df = q.df(f"SELECT ts, gexp FROM {table} WHERE gexp IS NOT NULL ORDER BY ts")
        self._dates = df["ts"].dt.strftime("%Y-%m-%d").tolist()
        self._vals = df["gexp"].astype(float).tolist()

    def gexp_prev(self, day: str) -> float | None:
        """CAUSAL prior-session GEX percentile for trading day 'YYYY-MM-DD':
        the gexp of the latest session STRICTLY BEFORE `day`."""
        import bisect
        i = bisect.bisect_left(self._dates, day)
        return self._vals[i - 1] if i > 0 else None

    def is_short_gamma(self, day: str, max_pctl: float = TREND_MAX_PCTL) -> bool | None:
        """True = short-gamma regime (trend sleeves on). None = no data."""
        v = self.gexp_prev(day)
        return None if v is None else v <= max_pctl


__all__ = ["GammaRegime", "TREND_MAX_PCTL"]
