"""GammaLevels — daily dealer-gamma STRIKE levels (put wall / call wall /
zero-gamma flip) from claude_gex_levels (CBOE true-OI, tools/fetch_cboe_gex.py).

Per-underlying: SPX rows map to ES, NDX rows to NQ. Rows written before the
NDX extension have underlying NULL and are treated as SPX.

CAUSAL: the levels for trading day D are the PRIOR session's EOD levels — the
standing option-OI structure the day opens into. Strikes are index (SPX/NDX)
points; the future trades at a ~basis above cash (ES ~ +52 over SPX measured on
the 2026 recorded window, std 6; NQ over NDX must be calibrated the same way:
future_close - index_close, then set it in config/instruments.yaml gex.basis).
The basis is a parameter because it drifts slowly with rates/time-to-expiry.

These are S/R candidates, not signals: the put wall tends to support and the
call wall to cap in LONG-gamma (net_sign>0, dealers pin); in SHORT-gamma the
walls fail more readily and price is amplified away from the flip (observed
2026-07-08, ES). The NDX/NQ mapping is the SAME MECHANISM but has NO validated
history yet — forward-collect first, eyeball the walls vs price, treat as
observation until a few weeks of sessions accumulate.
"""
from __future__ import annotations

import bisect

from ..adapters.questdb import QuestDB

DEFAULT_BASIS = 52.0        # SPX -> ES (legacy alias; per-instrument basis in yaml)


class GammaLevels:
    def __init__(self, q: QuestDB | None = None, table: str = "claude_gex_levels",
                 underlying: str = "SPX") -> None:
        q = q or QuestDB()
        self.underlying = underlying
        where = (f"WHERE underlying = '{underlying}'"
                 + (" OR underlying IS NULL" if underlying == "SPX" else ""))
        try:
            df = q.df(f"SELECT ts, put_wall, call_wall, zero_gamma, net_sign "
                      f"FROM {table} {where} ORDER BY ts")
        except Exception:
            # pre-migration table without the underlying column: it is all SPX
            if underlying != "SPX":
                raise
            df = q.df(f"SELECT ts, put_wall, call_wall, zero_gamma, net_sign "
                      f"FROM {table} ORDER BY ts")
        self._dates = df["ts"].dt.strftime("%Y-%m-%d").tolist()
        self._pw = df["put_wall"].tolist()
        self._cw = df["call_wall"].tolist()
        self._flip = df["zero_gamma"].tolist()
        self._net = df["net_sign"].tolist()

    def levels_prev(self, day: str, basis: float = DEFAULT_BASIS) -> dict | None:
        """Prior-session gamma levels in FUTURE terms (index strike + basis) for
        trading day 'YYYY-MM-DD', or None if no prior row."""
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
            underlying=self.underlying,
        )


__all__ = ["GammaLevels", "DEFAULT_BASIS"]
