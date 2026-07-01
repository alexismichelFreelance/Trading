"""Kaufman efficiency ratio (trend vs chop) on coarse bars.

ER = |net move over L bars| / sum(|bar-to-bar moves| over L bars), in [0,1].
~1 = clean trend, ~0 = chop. Look-ahead-free. The regime HMM consumes ER on
1-hour bars (L=3); the efficiency ratio is meaningless on 1s ticks (noise -> ~0)
and MUST be computed on coarse (>=15m) bars (a hard-won research lesson).
"""
from __future__ import annotations

import bisect
from collections import deque

import numpy as np

NS = 1_000_000_000


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


class RollingEfficiency:
    """CAUSAL Kaufman efficiency over a trailing TIME window anchored at the
    current second (no bar-close look-ahead) — the live-safe replacement for the
    1h regime HMM. Samples price at now, now-seg, ..., now-window (nseg segments)
    using the last price at/ before each sample time, within the SAME session.
    Returns None until the trailing window is fully covered this session.
    """

    def __init__(self, window_s: int = 7200, nseg: int = 3) -> None:
        self.window_ns = window_s * NS
        self.nseg = nseg
        self._ts: list[int] = []
        self._px: list[float] = []
        self._day: str | None = None

    def update(self, ts: int, px: float, day: str) -> float | None:
        if day != self._day:
            self._day = day
            self._ts.clear()
            self._px.clear()
        self._ts.append(ts)
        self._px.append(px)
        return self.value(ts)

    def _at(self, tk: int) -> float | None:
        j = bisect.bisect_right(self._ts, tk) - 1     # last sample <= tk
        return self._px[j] if j >= 0 else None

    def value(self, ts: int) -> float | None:
        if not self._ts or ts - self._ts[0] < self.window_ns:
            return None                                # window not covered this session
        seg = self.window_ns // self.nseg
        pts = []
        for k in range(self.nseg, -1, -1):
            p = self._at(ts - k * seg)
            if p is None:
                return None
            pts.append(p)
        net = abs(pts[-1] - pts[0])
        path = sum(abs(pts[i + 1] - pts[i]) for i in range(len(pts) - 1))
        return net / path if path > 0 else 0.0


__all__ = ["kaufman_er", "OnlineKaufmanER", "RollingEfficiency"]
