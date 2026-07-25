"""Multi-scale swing detection — the target structure for flow research.

Price is fractal: a large swing is built from smaller swings. Picking ONE
amplitude silently picks one timeframe and discards every other scale, so the
detectors here are always run over a LADDER of scales, and the scale is an
output of the analysis rather than an assumption baked into it.

Two things keep this honest:

* NO MAGIC NUMBERS. Amplitudes are expressed in units of each session's own
  volatility (`vol_unit`), not points, so a scale means the same thing on a
  quiet ES day, a wild NQ day, and a different year's feed. We measured flow
  magnitudes drifting ~5x across months while the sign held, so anything stated
  in absolute points is guaranteed to rot.

* ORACLE vs CAUSAL are different jobs and are kept apart. `zigzag` is an ORACLE
  detector: it reports the exact extreme, which is only knowable in hindsight.
  That is correct for labelling "what WOULD have been the perfect entry".
  A causal detector (see `directional_change`) only knows an extreme after
  price has reversed by delta, so it is late by construction -- and the gap
  between the two is itself a measurement: it bounds how much of the oracle
  edge is actually reachable in real time.

Detectors share one signature -- (px, amp) -> [(index, kind)] with kind=+1 for
a high and -1 for a low, strictly alternating -- so they are interchangeable.
"""
from __future__ import annotations

import numpy as np


def vol_unit(px: np.ndarray, horizon: int = 60) -> float:
    """The session's own scale: the median absolute price change over `horizon`
    samples. Median (not std) so a single spike cannot set the ruler. Every
    amplitude in this module is a multiple of this, which is what makes a scale
    comparable across sessions, instruments and feeds."""
    if len(px) <= horizon:
        return 0.0
    d = np.abs(px[horizon:] - px[:-horizon])
    d = d[np.isfinite(d)]
    return float(np.median(d)) if len(d) else 0.0


def zigzag(px: np.ndarray, amp: float) -> list[tuple[int, int]]:
    """ORACLE detector. Confirmed swing points as (index, kind), kind=+1 high,
    -1 low, strictly alternating. A high is confirmed once price falls `amp`
    below the running max; the reported index is the exact max.

    The running max and min MUST be tracked independently. A single 'extreme'
    that follows price in whichever direction it moves always equals the current
    price, so the `amp` separation can never build and the detector silently
    returns nothing at all."""
    n = len(px)
    if n < 3 or amp <= 0:
        return []
    piv: list[tuple[int, int]] = []
    hi_i = lo_i = 0
    hi_v = lo_v = px[0]
    direction = 0                       # 0 unknown, +1 seeking high, -1 seeking low
    for i in range(1, n):
        v = px[i]
        if v > hi_v:
            hi_i, hi_v = i, v
        if v < lo_v:
            lo_i, lo_v = i, v
        if direction >= 0 and v <= hi_v - amp:
            if not piv or piv[-1][1] != +1:
                piv.append((hi_i, +1))
                direction = -1
                hi_i, hi_v = lo_i, lo_v = i, v
        elif direction <= 0 and v >= lo_v + amp:
            if not piv or piv[-1][1] != -1:
                piv.append((lo_i, -1))
                direction = +1
                hi_i, hi_v = lo_i, lo_v = i, v
    return piv


def directional_change(px: np.ndarray, amp: float) -> list[tuple[int, int]]:
    """CAUSAL detector (Guillaume/Olsen 'intrinsic time'). Same ladder, but the
    event index is the bar where the reversal is CONFIRMED, not the extreme
    itself -- i.e. what a live system could actually have known. Comparing its
    outcomes against `zigzag` on the same scale measures the cost of that lag."""
    n = len(px)
    if n < 3 or amp <= 0:
        return []
    piv: list[tuple[int, int]] = []
    hi_v = lo_v = px[0]
    direction = 0
    for i in range(1, n):
        v = px[i]
        hi_v = max(hi_v, v)
        lo_v = min(lo_v, v)
        if direction >= 0 and v <= hi_v - amp:
            if not piv or piv[-1][1] != +1:
                piv.append((i, +1))            # confirmed HERE, not at the peak
                direction = -1
                hi_v = lo_v = v
        elif direction <= 0 and v >= lo_v + amp:
            if not piv or piv[-1][1] != -1:
                piv.append((i, -1))
                direction = +1
                hi_v = lo_v = v
    return piv


DETECTORS = {"zigzag": zigzag, "dc": directional_change}


def swing_outcomes(px: np.ndarray, piv: list[tuple[int, int]]) -> list[dict]:
    """Gain and risk for taking every swing, held to the next opposite pivot.

    For an entry at pivot i0 in direction d (low -> long, high -> short):
      mfe  best excursion in favour, points   -> the gain on offer
      mae  worst excursion against, points    -> the HEAT taken to get it (<=0)
      bars samples held                       -> the timeframe of that scale
    MAE is the number that decides whether a scale is tradeable: a 20pt move
    that first goes 15pt against you is not a 20pt opportunity."""
    out = []
    for (i0, k0), (i1, _k1) in zip(piv, piv[1:]):
        d = 1 if k0 == -1 else -1
        seg = px[i0:i1 + 1]
        if len(seg) < 2:
            continue
        rel = (seg - seg[0]) * d
        out.append({"i0": i0, "i1": i1, "dir": d,
                    "mfe": float(rel.max()), "mae": float(rel.min()),
                    "net": float(rel[-1]), "bars": int(i1 - i0)})
    return out


__all__ = ["vol_unit", "zigzag", "directional_change", "DETECTORS",
           "swing_outcomes"]
