"""Kaufman efficiency ratio (trend vs chop) on coarse bars.

ER = |net move over L bars| / sum(|bar-to-bar moves| over L bars), in [0,1].
~1 = clean trend, ~0 = chop. Look-ahead-free. The regime HMM consumes ER on
1-hour bars (L=3); the efficiency ratio is meaningless on 1s ticks (noise -> ~0)
and MUST be computed on coarse (>=15m) bars (a hard-won research lesson).
"""
from __future__ import annotations

from collections import deque

import numpy as np


def kaufman_er(closes, lookback: int) -> np.ndarray:
    """Vectorized ER aligned so er[i] uses closes[i-L .. i]; first L are NaN."""
    c = np.asarray(closes, dtype=float)
    n = len(c)
    er = np.full(n, np.nan)
    if n <= lookback:
        return er
    net = np.abs(c[lookback:] - c[:-lookback])
    absdiff = np.abs(np.diff(c))
    path = np.convolve(absdiff, np.ones(lookback), "valid")  # sum of L moves
    er[lookback:] = np.where(path > 0, net / path, 0.0)
    return er


class OnlineKaufmanER:
    """Incremental ER over the last L+1 closes. Returns None until warm."""

    def __init__(self, lookback: int = 3) -> None:
        self.L = lookback
        self._c: deque[float] = deque(maxlen=lookback + 1)

    def update(self, close: float) -> float | None:
        self._c.append(close)
        if len(self._c) < self.L + 1:
            return None
        cs = list(self._c)
        net = abs(cs[-1] - cs[0])
        path = sum(abs(cs[i + 1] - cs[i]) for i in range(len(cs) - 1))
        return net / path if path > 0 else 0.0


__all__ = ["kaufman_er", "OnlineKaufmanER"]
