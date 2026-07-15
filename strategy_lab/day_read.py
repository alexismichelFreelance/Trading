"""Compute the DayScore fade-friendliness read: validate it separates rotational
from trending days (and lifts fade capture), then print TODAY's read.

    python strategy_lab/day_read.py            # validate + today's read
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
sys.path.insert(0, "D:/Trading/strategy_lab")
from scipy.stats import spearmanr

from engine.adapters.questdb import QuestDB
from engine.features.day_score import DayScore, pctl

q = QuestDB(timeout=180)
TRAIL = 15


def sess(hr):
    if hr >= 18 or hr < 3:
        return "asia"
    if 3 <= hr < 9:
        return "eu"
    return "us"


def daily_aggs(B, has_overnight):
    """Per US-day raw aggregates (causal features + the outcome)."""
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
        on = g[g.h_et.map(sess) != "us"]
        rows.append(dict(
            day=d, rth_range=rth.h.max() - rth.l.min(),
            overnight_range=(on.h.max() - on.l.min()) if (has_overnight and len(on) > 30) else None,
            fh_vol=early.vol.sum() if len(early) else np.nan,
            e_below=e_below, onesided=onesided))
    return pd.DataFrame(rows).sort_values("day").reset_index(drop=True)


def score_days(F):
    """Add causal trailing percentiles + the fade_friendliness score."""
    out = []
    for i, r in F.iterrows():
        hist = F.iloc[max(0, i - TRAIL - 1):i]
        crabel = pctl(F.rth_range.iloc[i - 1], hist.rth_range.tolist()) if i >= 6 else None
        overn = pctl(r.overnight_range, hist.overnight_range.tolist()) \
            if r.overnight_range is not None else None
        rv = pctl(r.fh_vol, hist.fh_vol.tolist())
        volcalm = (1 - rv) if rv is not None else None
        s = DayScore.fade_friendliness(crabel=crabel, overnight=overn, volcalm=volcalm)
        out.append(dict(day=r.day, score=s, crabel=crabel, overnight=overn,
                        volcalm=volcalm, e_below=r.e_below, onesided=r.onesided))
    return pd.DataFrame(out)


def validate(tag, S):
    S = S.dropna(subset=["score", "onesided"])
    if len(S) < 10:
        print(f"{tag}: only {len(S)} scored days"); return
    rho, p = spearmanr(S.score, S.onesided)
    hi = S[S.score >= S.score.median()]; lo = S[S.score < S.score.median()]
    print(f"{tag}: {len(S)} days  corr(score, one-sidedness)={rho:+.2f} (p={p:.2f}; "
          f"negative = high score -> rotational, as intended)")
    print(f"    high-score days: one-sidedness {hi.onesided.mean():.2f}  |  "
          f"low-score days: {lo.onesided.mean():.2f}")


if __name__ == "__main__":
    live = q.df("SELECT ts,o,h,l,c,vol FROM claude_bars_live ORDER BY ts")
    S26 = score_days(daily_aggs(live, has_overnight=True))
    validate("2026 recorded (24h)", S26)
    r25 = q.df("SELECT symbol,ts,o,h,l,c,vol FROM claude_bars_1m ORDER BY ts")
    et = r25.ts.dt.tz_convert("America/New_York"); d = et.dt.strftime("%Y-%m-%d")
    r25 = r25[~(((r25.symbol == "ESH5") & (d >= "2025-03-20")) |
               ((r25.symbol == "ESM5") & (d < "2025-03-20")))]
    validate("2025 research (crabel+volcalm only)", score_days(daily_aggs(r25, has_overnight=False)))

    print("\n=== TODAY'S READ ===")
    last = S26.iloc[-1]
    print(f"  {last.day}   fade-friendliness: "
          f"{last.score:.0f}/100  [{DayScore.label(last.score)}]")
    comp = {k: last[k] for k in ("crabel", "overnight", "volcalm") if pd.notna(last[k])}
    print("  components (high=rotational): " +
          "  ".join(f"{k} {v:.0%}" for k, v in comp.items()))
    print(f"  posture: {DayScore.posture(last.e_below)}")
