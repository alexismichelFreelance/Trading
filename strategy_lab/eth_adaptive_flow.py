"""Adaptive flow on the OVERNIGHT (ETH) session — does the scale-invariant
z-score threshold find an edge the fixed/rescaled versions couldn't?

ETH sessions = 21:00 UTC (day D) -> 13:00 UTC (D+1), keyed by the morning date
(complement of the 13-21 RTH window). Adaptive th_t = rolling_mean + k*std of
|adelta| over a trailing 30-min window (per-session reset, 5-min warmup);
scale_t = 15*th_t. Data: claude_sec_eth (both contracts).

ROBUSTNESS FIRST (the old ETH rescale was a mirage): report net at cost 0.30 AND
0.60 (wider overnight spread), per-month signs, and per-NIGHT concentration
(top-5 nights % of total — >~40% = mirage). Compare k values + the RTH number.
"""
import sys
from collections import defaultdict, deque

import numpy as np
import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB

q = QuestDB(timeout=180)
W, A, H, MAXP = 120, 1, 5, 50
VOL_WIN, WARM = 1800, 300


def jsround(x):
    return int(np.floor(x + 0.5))


def sign(x):
    return int(x > 0) - int(x < 0)


# ── load ETH per-second, build overnight sessions on a 1s grid ────────────
rows = []
for d in q.df("SELECT DISTINCT to_str(ts,'yyyy-MM-dd') d FROM claude_sec_eth ORDER BY d")["d"]:
    rows.append(q.df(f"SELECT ts, pxc, adelta FROM claude_sec_eth "
                     f"WHERE ts >= '{d}T00:00:00.000000Z' AND ts < '{d}T23:59:59.999999Z' ORDER BY ts"))
E = pd.concat(rows, ignore_index=True)
E["sess"] = np.where(E.ts.dt.hour >= 21,
                     (E.ts + pd.Timedelta(hours=13)).dt.strftime("%Y-%m-%d"),
                     E.ts.dt.strftime("%Y-%m-%d"))
sessions = {}
for k, g in E.groupby("sess"):
    g = g.sort_values("ts")
    start, end = g.ts.min().floor("s"), g.ts.max().ceil("s")
    if (end - start) < pd.Timedelta(hours=8):
        continue
    grid = pd.date_range(start, end, freq="1s")
    ad = g.set_index("ts")["adelta"].reindex(grid, fill_value=0).to_numpy()
    px = g.set_index("ts")["pxc"].reindex(grid).ffill().bfill().to_numpy()
    sessions[k] = (ad, px)
print(f"ETH sessions on 1s grid: {len(sessions)}")


def run_adaptive(k):
    nightly = {}
    for key, (ad, px) in sessions.items():
        absad = pd.Series(np.abs(ad))
        m = absad.rolling(VOL_WIN, min_periods=WARM).mean().to_numpy()
        sd = absad.rolling(VOL_WIN, min_periods=WARM).std().to_numpy()
        th = m + k * sd
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
            sc = 15.0 * t
            tgt = 0.0 if (np.isnan(sc) or sc <= 0) else max(-MAXP, min(MAXP, F / sc))
            delta = tgt - held
            band = A if held == 0 else (A if sign(delta) == sign(held) else H)
            if abs(delta) > band:
                nv = jsround(tgt)
                turn += abs(nv - held)
                held = nv
            if i < n - 1:
                pnl += held * (px[i + 1] - px[i])
        nightly[key] = (pnl, turn)
    return nightly


def summarize(k):
    nightly = run_adaptive(k)
    for cost in (0.30, 0.60):
        net = {key: pnl - turn * cost for key, (pnl, turn) in nightly.items()}
        v = np.array(sorted(net.values())[::-1])
        tot = v.sum()
        bym = defaultdict(float)
        for key, x in net.items():
            bym[key[:7]] += x
        months_pos = all(x > 0 for x in bym.values())
        top5 = v[:5].sum()
        conc = 100 * top5 / tot if tot > 0 else 999
        pm = " ".join(f"{mm[-2:]}:{mv:+.0f}" for mm, mv in sorted(bym.items()))
        print(f"  k={k} cost={cost:.2f}: net={tot:+8.0f}  win_nights={100*(v>0).mean():4.0f}%  "
              f"top5={conc:+4.0f}%ofTot  months+={str(months_pos):5}  | {pm}")


print("\nadaptive flow on ETH (mirage check: top5% should be modest, months+ true):")
for k in (3, 4, 5):
    summarize(k)
print("\nreference: RTH adaptive k=4 was +939 (oracle) / +701 (replay), all-months+;"
      " old ETH FIXED-rescale mirage: top5=719% of total, 3/4 months negative.")
