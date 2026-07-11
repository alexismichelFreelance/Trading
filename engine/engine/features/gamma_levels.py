"""GammaLevels — daily dealer-gamma STRIKE levels (put wall / call wall /
zero-gamma flip) from claude_gex_levels (CBOE true-OI, tools/fetch_cboe_gex.py).

CAUSAL: the levels for trading day D are the PRIOR session's EOD levels — the
standing option-OI structure the day opens into. Strikes are SPX; ES trades at
a ~basis above SPX (measured ~+52pt on the 2026 recorded window, std 6). The
basis is a parameter because it drifts slowly with rates/time-to-expiry;
recalibrate from ES_close - SqueezeMetrics spx_close periodically.

These are S/R candidates, not signals: the put wall tends to support and the
call wall to cap in LONG-gamma (net_sign>0, dealers pin); in SHORT-gamma the
walls fail more readily and price is amplified away from the flip (observed
2026-07-08). See gamma/GEX_LEVELS.md.
"""
from __future__ import annotations

import bisect

from ..adapters.questdb import QuestDB

DEFAULT_BASIS = 52.0


class GammaLevels:
    def __init__(self, q: QuestDB | None = None, table: str = "claude_gex_levels") -> None:
        q = q or QuestDB()
        df = q.df(f"SELECT ts, put_wall, call_wall, zero_gamma, net_sign FROM {table} "
                  f"ORDER BY ts")
        self._dates = df["ts"].dt.strftime("%Y-%m-%d").tolist()
        self._pw = df["put_wall"].tolist()
        self._cw = df["call_wall"].tolist()
        self._flip = df["zero_gamma"].tolist()
        self._net = df["net_sign"].tolist()

    def levels_prev(self, day: str, basis: float = DEFAULT_BASIS) -> dict | None:
        """Prior-session gamma levels in ES terms for trading day 'YYYY-MM-DD',
        or None if no prior row."""
        i = bisect.bisect_left(self._dates, day)
        if i == 0:
            return None
        j = i - 1
        flip = self._flip[j]
        return dict(
            sess=self._dates[j],
            put_wall=self._pw[j] + basis,
            call_wall=self._cw[j] + basis,
            flip=(flip + basis) if flip is not None and flip == flip else None,
            net_sign=int(self._net[j]),
            basis=basis,
        )


__all__ = ["GammaLevels", "DEFAULT_BASIS"]
