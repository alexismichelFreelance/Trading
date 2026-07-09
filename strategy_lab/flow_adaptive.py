"""Adaptive-flow study: replace flow's magic absolute th=200 with a threshold
that MODULATES with current flow conditions, so it's scale-invariant (like
ignition's relative strength) and fires on any feed.

Laws tested (th_t computed causally from a trailing 30-min window of |adelta|,
reset per ET day, 5-min warmup):
  FIXED   th=200                      (baseline reference, the only all-months+)
  MEAN    th = k * roll_mean          (mean-relative; k~13 == th=200 on research)
  ZSCORE  th = roll_mean + k*roll_std (adapts to distribution SHAPE, not just level)
scale_t = 15 * th_t (keep the validated 15:1 ratio). Cost = 0.30/turnover (the
flow parity model). Report total net_pts, per-month, all-months-positive (both
contracts pooled), and the typical th_t (does it fire on a live-like feed?).
"""
import sys
from collections import defaultdict, deque

import numpy as np
import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB

q = QuestDB(timeout=180)
W, A, H, MAXP, COST = 120, 1, 5, 50, 0.30
VOL_WIN, WARM = 1800, 300         # 30-min trailing vol estimate, 5-min warmup


def jsround(x):
    return int(np.floor(x + 0.5))


def sign(x):
    return int(x > 0) - int(x < 0)


def load(sym, tbl):
    df = q.df(f"SELECT ts, adelta, pxc FROM {tbl} ORDER BY ts")
    df["day"] = df["ts"].dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    df["month"] = df["ts"].dt.strftime("%Y-%m")
    return df


def th_series(absad: pd.Series, law: str, k: float):
    m = absad.rolling(VOL_WIN, min_periods=WARM).mean()
    if law == "MEAN":
        return k * m
    if law == "ZSCORE":
        sd = absad.rolling(VOL_WIN, min_periods=WARM).std()
        return m + k * sd
    return pd.Series(k, index=absad.index)      # FIXED


def run(df, law, k):
    net = defaultdict(float)
    th_samples = []
    for (day, month), g in df.groupby(["day", "month"]):
        g = g.sort_values("ts").reset_index(drop=True)
        grid = pd.date_range(g["ts"].min().floor("s"), g["ts"].max().ceil("s"), freq="1s")
        ad = g.set_index("ts")["adelta"].reindex(grid, fill_value=0).to_numpy()
        px = g.set_index("ts")["pxc"].reindex(grid).ffill().bfill().to_numpy()
        absad = pd.Series(np.abs(ad))
        th = th_series(absad, law, k).to_numpy()
        scale = 15.0 * th
        th_samples.append(th[~np.isnan(th)])
        F = 0.0
        buf = deque()
        held = 0
        pnl = 0.0
        turn = 0.0
        n = len(ad)
        for i in range(n):
            t = th[i]
            x = ad[i] if (not np.isnan(t) and abs(ad[i]) >= t) else 0
            buf.append(x)
            F += x
            if len(buf) > W:
                F -= buf.popleft()
            sc = scale[i]
            tgt = 0.0 if (np.isnan(sc) or sc <= 0) else max(-MAXP, min(MAXP, F / sc))
            delta = tgt - held
            band = A if held == 0 else (A if sign(delta) == sign(held) else H)
            if abs(delta) > band:
                nv = jsround(tgt)
                turn += abs(nv - held)
                held = nv
            if i < n - 1:
                pnl += held * (px[i + 1] - px[i])
        net[month] += pnl - turn * COST
    th_all = np.concatenate(th_samples) if th_samples else np.array([np.nan])
    return net, np.nanmedian(th_all)


DATA = {s: load(s, t) for s, t in (("ESM5", "claude_sec_feat"), ("ESH5", "claude_sec_feat_esh5"))}

print(f"{'law':<8}{'k':>5}{'net':>7}{'pool+':>7}{'perC+':>6}{'medth':>6}  per-contract (ESM5 | ESH5)")
CONFIGS = [("FIXED", 200)] + [("ZSCORE", k) for k in (3, 3.5, 4, 4.5, 5, 5.5, 6, 7)]
for law, k in CONFIGS:
    pooled = defaultdict(float)
    per_c = {}
    med_ths = []
    for sym in DATA:
        net, med = run(DATA[sym], law, k)
        per_c[sym] = net
        for m, v in net.items():
            pooled[m] += v
        med_ths.append(med)
    total = sum(pooled.values())
    pool_pos = all(v > 0 for v in pooled.values())
    perc_pos = all(all(v > 0 for v in per_c[s].values()) for s in DATA)
    em = " ".join(f"{m[-2:]}:{v:+.0f}" for m, v in sorted(per_c["ESM5"].items()))
    eh = " ".join(f"{m[-2:]}:{v:+.0f}" for m, v in sorted(per_c["ESH5"].items()))
    print(f"{law:<8}{k:>5}{total:>+7.0f}{str(pool_pos):>7}{str(perc_pos):>6}{np.mean(med_ths):>6.0f}  {em} | {eh}")
