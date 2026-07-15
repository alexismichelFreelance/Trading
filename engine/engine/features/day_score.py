"""DayScore — a daily 'fade-friendliness' read that combines the moderate,
causal day-character tilts we validated (LITERATURE_DAY_TYPE.md) into ONE number.

No single signal is a clean switch (|rho| <= 0.45, EMH-consistent), so this is a
SIZING / co-pilot input — "press harder / lighter today" — not a trade gate.

Components (each mapped to [0,1] where HIGH = more rotational = fade-friendly):
  crabel    prior-day RTH range percentile      (wide prior day -> rotational; rho -0.36)
  overnight Asia+Europe range percentile         (active overnight -> rotational US; rho -0.25)
  volcalm   1 - first-hour relative-volume pctl  (low AM volume -> rotational; Gao)
fade_friendliness = weighted mean * 100. Missing components are dropped and the
weights renormalised, so the score is usable at the open (crabel only), better by
09:30 (+overnight), best by 10:30 (+volume).

Separately, POSTURE (directional, from the persistent first-hour VWAP side) says
WHICH way to lean: VWAP as support (buy dips) vs resistance (fade rallies).
"""
from __future__ import annotations

import numpy as np

WEIGHTS = {"crabel": 0.45, "overnight": 0.30, "volcalm": 0.25}


def pctl(value: float, history: list[float]) -> float | None:
    """Fraction of the trailing history strictly below `value` (needs >=5)."""
    h = [x for x in history if x is not None and not np.isnan(x)]
    if value is None or (isinstance(value, float) and np.isnan(value)) or len(h) < 5:
        return None
    return float(np.mean(np.array(h) < value))


class DayScore:
    @staticmethod
    def fade_friendliness(crabel=None, overnight=None, volcalm=None,
                          weights=WEIGHTS) -> float | None:
        """0-100; high = rotational/mean-reverting (fade-friendly). Each input is
        a [0,1] tilt (high = rotational) or None if not yet available."""
        parts = {"crabel": crabel, "overnight": overnight, "volcalm": volcalm}
        avail = {k: v for k, v in parts.items() if v is not None}
        if not avail:
            return None
        w = sum(weights[k] for k in avail)
        return 100.0 * sum(weights[k] * avail[k] for k in avail) / w

    @staticmethod
    def label(score: float | None) -> str:
        if score is None:
            return "n/a"
        if score >= 65:
            return "PRESS (rotational, fade-friendly)"
        if score >= 45:
            return "normal"
        return "LIGHT (trend risk, fade carefully)"

    @staticmethod
    def posture(early_frac_below: float | None) -> str:
        """From the persistent first-hour VWAP side (+0.45 corr)."""
        if early_frac_below is None:
            return "unknown"
        if early_frac_below < 0.40:
            return "VWAP=support -> buy dips to VWAP/-band"
        if early_frac_below > 0.60:
            return "VWAP=resistance -> fade rallies to VWAP/+band"
        return "balanced -> fade both sides"


__all__ = ["DayScore", "pctl", "WEIGHTS"]
