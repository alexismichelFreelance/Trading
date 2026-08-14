"""The index->future basis, measured from stored data every session.

A gamma level is an index strike (SPX/NDX); the sleeves trade a future. The
basis is what puts the level on the chart, and it was a constant in
config/instruments.yaml -- ES 42.0, NQ 181.0 -- each measured once on
2026-07-20 with a comment saying to re-measure. Nothing re-measured them,
because a comment is not a mechanism.

Measured over 13 stored sessions:

    ES   47.6  41.2  36.3  34.9  32.4 ... 20.9  20.3  20.0  22.5
    NQ  225.1 184.5 180.3 148.2 133.7 ... 112.4 110.0  98.6  99.5

Monotonic DECAY: futures carry converging to cash as expiry approaches. It then
JUMPS at the roll and decays again. Against the configured values, today ES is
~20 points wrong and NQ ~80 -- so every NQ wall and flip has been drawn 80
points from where it belongs, worsening daily. No re-measurement cadence fixes
that; a constant is the wrong SHAPE for the quantity.

Both inputs are already stored: the CBOE payload's `spot` (the index close, and
empirically the PRIOR session's -- dispersion collapses from std 51 to 11 on ES
under that alignment) and our own RTH futures close. Same session, so the basis
is a subtraction rather than an estimate.
"""
from __future__ import annotations

import logging
import statistics

log = logging.getLogger("engine.gamma")

WINDOW = 3          # sessions in the median
MAX_JUMP = 25.0     # a step larger than this, in %, is a ROLL not a drift


class BasisSeries:
    """Per-session basis with a short median, roll-aware."""

    def __init__(self, rows: list, fallback: float, symbol: str = "") -> None:
        # rows: [(day, index_close, future_close)] ascending
        self.symbol = symbol
        self.fallback = float(fallback)
        self._days = [d for d, _, _ in rows]
        self._vals = [f - i for _, i, f in rows]
        self.measured = bool(self._vals)
        self._warned = False

    @classmethod
    def from_rows(cls, rows, fallback: float, symbol: str = "") -> "BasisSeries":
        return cls(sorted(rows), fallback, symbol)

    @classmethod
    def load(cls, q, underlying: str, fut: str, fallback: float,
             days: int = 30) -> "BasisSeries":
        """Join the stored index closes to our own RTH futures closes."""
        import pandas as pd
        try:
            gx = q.df(f"SELECT ts, spot FROM claude_gex_levels "
                      f"WHERE underlying = '{underlying}' ORDER BY ts")
            b = q.df(f"SELECT ts, c FROM claude_bars_live WHERE symbol = '{fut}' "
                     f"ORDER BY ts")
        except Exception as ex:                       # noqa: BLE001
            log.warning("basis unmeasurable for %s (%s); falling back to the "
                        "configured constant %.1f", fut, ex, fallback)
            return cls([], fallback, fut)
        if not len(gx) or not len(b):
            return cls([], fallback, fut)
        et = pd.to_datetime(b["ts"], utc=True).dt.tz_convert("America/New_York")
        m = et.dt.hour * 60 + et.dt.minute
        b = b[(m >= 570) & (m < 960)].copy()
        b["day"] = et[(m >= 570) & (m < 960)].dt.strftime("%Y-%m-%d")
        close = b.groupby("day")["c"].last()
        gx["day"] = gx["ts"].dt.strftime("%Y-%m-%d")
        # The payload's `spot` is the PRIOR session's index close, so it pairs
        # with the PRIOR session's futures close -- same day, both sides.
        rows = []
        idx = sorted(close.index)
        for r in gx.itertuples():
            prev = [d for d in idx if d < r.day]
            if prev:
                rows.append((prev[-1], float(r.spot), float(close[prev[-1]])))
        return cls(sorted(set(rows))[-days:], fallback, fut)

    def for_day(self, day: str) -> float:
        """Basis to use for trading day `day`, from sessions strictly before it."""
        vals = [v for d, v in zip(self._days, self._vals) if d < day]
        if not vals:
            vals = list(self._vals)                   # nothing older: use what we have
        if not vals:
            if not self._warned:
                self._warned = True
                log.warning("no measurable %s basis; using the CONFIGURED %.1f. "
                            "That constant was a single reading of a decaying "
                            "series and every level built on it is offset.",
                            self.symbol or "index->future", self.fallback)
            return self.fallback
        # A ROLL changes the front contract and the basis STEPS. Averaging over
        # the step describes neither contract, so only the sessions since the
        # last step count.
        # ...but a roll and a BAD PRINT are the same size of step. What tells
        # them apart is persistence: a roll HOLDS at the new level, an outlier
        # reverts. So a jump counts only once the session after it confirms the
        # new level. An unconfirmed jump at the very end is left alone and the
        # median below absorbs it -- a genuine roll then takes one more session
        # to register, which is the right way round: a level that is one session
        # late beats a level thrown 700 points by a single bad close.
        cut = 0
        for i in range(1, len(vals) - 1):
            prev, cur, nxt = vals[i - 1], vals[i], vals[i + 1]
            if abs(prev) < 1e-9:
                continue
            jumped = abs(cur - prev) / abs(prev) * 100.0 > MAX_JUMP
            held = abs(cur) < 1e-9 or abs(nxt - cur) / abs(cur) * 100.0 <= MAX_JUMP
            if jumped and held:
                cut = i
        vals = vals[cut:]
        # median, not mean: one bad close must not move a level 100 points,
        # while a genuine few-points-a-day decay still comes through.
        return float(statistics.median(vals[-WINDOW:]))

    def latest(self) -> float | None:
        return self._vals[-1] if self._vals else None


__all__ = ["BasisSeries", "WINDOW"]
