"""Differentiate VWAP-RESPECTING (range) days from VWAP-TRAVERSING (trend) days,
and find what the FIRST HOUR predicts about which kind it'll be.

Observation-first. Outcome (whole RTH): how one-sided is the day around VWAP and
how efficient (trending) is it. Early features (first 60m, known by 10:30 ET):
does the first hour already trend / sit one-sided / traverse VWAP once? Then:
- do the early features separate the day types?
- if we fade ONLY on predicted-range days, does range_capture clear the 20% bar?
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
sys.path.insert(0, "D:/Trading/strategy_lab")
from engine.adapters.questdb import QuestDB

from range_capture import gamma_levels, load_2025, load_2026, run_scale

q = QuestDB(timeout=180)
EARLY_END = 570 + 60          # 10:30 ET


def vwap_series(g):
    v = g.vol.to_numpy().astype(float); c = g.c.to_numpy()
    cv = np.cumsum(v)
    vw = np.cumsum(c * v) / np.maximum(cv, 1)
    return vw


def eff(c):
    path = np.abs(np.diff(c)).sum()
    return abs(c[-1] - c[0]) / path if path > 0 else 0.0


def crossings(c, vw):
    s = np.sign(c - vw)
    return int((np.abs(np.diff(s)) > 0).sum())


def features(df):
    rows = []
    for d, g in df.groupby("day"):
        g = g.sort_values("mod")
        if len(g) < 200:
            continue
        c = g.c.to_numpy(); mod = g["mod"].to_numpy()
        vw = vwap_series(g)
        early = mod < EARLY_END
        rest = mod >= EARLY_END
        if early.sum() < 30 or rest.sum() < 60:
            continue
        # OUTCOME (rest of day): one-sidedness around VWAP + trend efficiency
        below_rest = float(np.mean(c[rest] < vw[rest]))
        onesided_rest = max(below_rest, 1 - below_rest)       # 0.5 range .. 1.0 trend
        eff_rest = eff(c[rest])
        # EARLY features (first hour)
        below_e = float(np.mean(c[early] < vw[early]))
        rows.append(dict(
            day=d,
            e_onesided=max(below_e, 1 - below_e),
            e_cross=crossings(c[early], vw[early]),
            e_eff=eff(c[early]),
            e_range=g.h[early].max() - g.l[early].min(),
            onesided_rest=onesided_rest,
            eff_rest=eff_rest,
            below_rest=below_rest,
            trend_day=int(onesided_rest >= 0.80 or eff_rest >= 0.30),
        ))
    return pd.DataFrame(rows)


def observe(tag, F):
    print(f"\n=== {tag}: {len(F)} days ===")
    print(f"trend days (rest one-sided>=0.80 or eff>=0.30): {F.trend_day.mean():.0%}   "
          f"range days: {1-F.trend_day.mean():.0%}")
    from scipy.stats import spearmanr
    print("early feature -> rest-of-day one-sidedness (does the first hour foretell it?):")
    for f in ("e_onesided", "e_cross", "e_eff", "e_range"):
        rho, p = spearmanr(F[f], F.onesided_rest, nan_policy="omit")
        rho2, _ = spearmanr(F[f], F.eff_rest, nan_policy="omit")
        print(f"  {f:12s} vs onesided_rest rho={rho:+.2f} (p={p:.2f}) | vs eff_rest rho={rho2:+.2f}")
    print("means by day type:")
    for lbl, sub in (("range days", F[F.trend_day == 0]), ("trend days", F[F.trend_day == 1])):
        if len(sub):
            print(f"  {lbl:11s}: e_onesided {sub.e_onesided.mean():.2f}  "
                  f"e_cross {sub.e_cross.mean():.1f}  e_eff {sub.e_eff.mean():.2f}  "
                  f"e_range {sub.e_range.mean():.0f}")


if __name__ == "__main__":
    gam = gamma_levels()
    for tag, load in (("2026 recorded", load_2026), ("2025 research", load_2025)):
        df = load()
        F = features(df)
        observe(tag, F)
        # payoff: gate the scaling fade to PREDICTED-range days (first hour not
        # one-sided AND not already trending) and re-score capture
        R = run_scale(df, gam)
        M = R.merge(F[["day", "e_onesided", "e_eff", "trend_day"]], on="day", how="inner")
        pred_range = (M.e_onesided <= 0.70) & (M.e_eff <= 0.45)
        for lbl, sub in (("ALL days", M), ("predicted-RANGE only", M[pred_range]),
                         ("predicted-trend", M[~pred_range])):
            if len(sub):
                print(f"  {tag} {lbl:22s} n={len(sub):2d}  median capture "
                      f"{np.median(sub.pct)*100:+.0f}%  P(>=20%)={np.mean(sub.pct>=.2):.0%}  "
                      f"mean {np.mean(sub.pct)*100:+.0f}%")
