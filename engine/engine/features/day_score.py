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
import pandas as pd

WEIGHTS = {"crabel": 0.45, "overnight": 0.30, "volcalm": 0.25}
TRAIL = 15


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


# ── feature extraction from bars (shared by day_read.py + run_live) ─────────
def _sess(hr: int) -> str:
    if hr >= 18 or hr < 3:
        return "asia"
    if 3 <= hr < 9:
        return "eu"
    return "us"


def daily_aggregates(B: pd.DataFrame, has_overnight: bool = True) -> pd.DataFrame:
    """Per US-day raw features (causal) + the one-sidedness outcome. B has
    ts,o,h,l,c,vol; evening bars (>=18:00 ET) roll to the next day's session."""
    et = B.ts.dt.tz_convert("America/New_York")
    B = B.assign(h_et=et.dt.hour, mod=et.dt.hour * 60 + et.dt.minute)
    B["skey"] = np.where(B.h_et >= 18, (et + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d"),
                         et.dt.strftime("%Y-%m-%d"))
    rows = []
    for d, g in B.groupby("skey"):
        rth = g[(g["mod"] >= 570) & (g["mod"] < 960)]
        if len(rth) < 250:
            continue
        c = rth.c.to_numpy(); v = rth.vol.to_numpy().astype(float)
        cv = np.cumsum(v); vw = np.cumsum(c * v) / np.maximum(cv, 1)
        onesided = max(np.mean(c < vw), 1 - np.mean(c < vw))
        early = rth[rth["mod"] < 630]
        e_below = float(np.mean(early.c.to_numpy() < vw[:len(early)])) if len(early) else None
        on = g[g.h_et.map(_sess) != "us"]
        rows.append(dict(
            day=d, rth_range=rth.h.max() - rth.l.min(),
            overnight_range=(on.h.max() - on.l.min()) if (has_overnight and len(on) > 30) else None,
            fh_vol=early.vol.sum() if len(early) else np.nan,
            e_below=e_below, onesided=onesided))
    return pd.DataFrame(rows).sort_values("day").reset_index(drop=True)


def score_history(F: pd.DataFrame, trail: int = TRAIL) -> pd.DataFrame:
    """Add causal trailing percentiles + the fade_friendliness score per day."""
    out = []
    for i, r in F.iterrows():
        hist = F.iloc[max(0, i - trail - 1):i]
        crabel = pctl(F.rth_range.iloc[i - 1], hist.rth_range.tolist()) if i >= 6 else None
        overn = pctl(r.overnight_range, hist.overnight_range.tolist()) \
            if r.overnight_range is not None else None
        rv = pctl(r.fh_vol, hist.fh_vol.tolist())
        volcalm = (1 - rv) if rv is not None else None
        out.append(dict(day=r.day, score=DayScore.fade_friendliness(
            crabel=crabel, overnight=overn, volcalm=volcalm),
            crabel=crabel, overnight=overn, volcalm=volcalm,
            e_below=r.e_below, onesided=r.onesided))
    return pd.DataFrame(out)


def latest_read(B: pd.DataFrame, has_overnight: bool = True) -> dict | None:
    """The last COMPLETE session's full read (score + posture), or None."""
    F = daily_aggregates(B, has_overnight)
    if len(F) < 6:
        return None
    last = score_history(F).iloc[-1]
    comp = {k: last[k] for k in ("crabel", "overnight", "volcalm") if pd.notna(last[k])}
    return dict(day=last.day, score=last.score, label=DayScore.label(last.score),
                components=comp, posture=DayScore.posture(last.e_below))


def morning_read(B: pd.DataFrame) -> dict | None:
    """TODAY's PRE-MARKET lean: Crabel prior-range (knowable at the open) + this
    session's overnight range if recorded. Volume/posture come after the first
    hour — a partial score, deliberately. Falls back to the last complete
    session's read if today's session hasn't started."""
    et = B.ts.dt.tz_convert("America/New_York")
    B = B.assign(h_et=et.dt.hour)
    B["skey"] = np.where(B.h_et >= 18, (et + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d"),
                         et.dt.strftime("%Y-%m-%d"))
    F = daily_aggregates(B, has_overnight=True)
    if len(F) < 7:
        return None
    today = B.skey.max()
    prior = F[F.day < today]
    if today in set(F.day) or len(prior) < 6:        # today already complete -> full read
        return latest_read(B)
    hist = prior.tail(TRAIL + 1)
    crabel = pctl(hist.rth_range.iloc[-1], hist.rth_range.iloc[:-1].tolist())
    tg = B[(B.skey == today) & (B.h_et.map(_sess) != "us")]
    overn = pctl(tg.h.max() - tg.l.min(),
                 prior.overnight_range.dropna().tail(TRAIL).tolist()) if len(tg) > 30 else None
    score = DayScore.fade_friendliness(crabel=crabel, overnight=overn)
    comp = {k: v for k, v in (("crabel", crabel), ("overnight", overn)) if v is not None}
    return dict(day=today, score=score, label=DayScore.label(score),
                components=comp, posture="(develops after the first hour)", premarket=True)


__all__ = ["DayScore", "pctl", "WEIGHTS", "daily_aggregates", "score_history",
           "latest_read", "morning_read"]
