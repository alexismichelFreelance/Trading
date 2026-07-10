"""FORMING-BASE FADE — the user's demonstrated trade, mechanized causally.

Long side (shorts mirrored):
  FLUSH: at minute t0, low = 2h local low AND (max(high) over prior 30m − low) >= F
  BASE : minutes t0+1 .. t0+M hold the low (no new low) AND base range <= 0.4*F
  ENTRY: close of t0+M. Stop = flush_low − 1. Half off at +4 (then stop→BE);
         runner target = entry + 0.5*F; time stop 120m; MOC 15:59 ET.
P&L per trade = mean of the two halves − cost (0.517). One position at a time.
Datasets: 2025 research months (claude_bars_1m) AND 2026 recorded weeks
(claude_bars_live, RTH 9:30–16:00 ET). Full small grid reported (F×M).
Anchors: must catch the user's 2026-07-08 ~7492 long and 2026-07-10 ~7586 long.
"""
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB

q = QuestDB(timeout=180)
COST = 0.517
SCALP = 4.0


def load_2025():
    df = q.df("SELECT symbol, ts, o, h, l, c FROM claude_bars_1m ORDER BY ts")
    et = df.ts.dt.tz_convert("America/New_York")
    df["day"] = et.dt.strftime("%Y-%m-%d")
    df["mod"] = et.dt.hour * 60 + et.dt.minute
    df = df[(df["mod"] >= 570) & (df["mod"] < 960)]
    df = df[~((df.symbol == "ESH5") & (df.day >= "2025-03-20"))]
    df = df[~((df.symbol == "ESM5") & (df.day < "2025-03-20"))]
    return df


def load_2026():
    df = q.df("SELECT ts, o, h, l, c FROM claude_bars_live ORDER BY ts")
    et = df.ts.dt.tz_convert("America/New_York")
    df["day"] = et.dt.strftime("%Y-%m-%d")
    df["mod"] = et.dt.hour * 60 + et.dt.minute
    return df[(df["mod"] >= 570) & (df["mod"] < 960)]


def run(df, F, M, r_frac=0.4, anchors=None):
    trades = []
    hits = []
    for day, g in df.groupby("day"):
        g = g.sort_values("mod").reset_index(drop=True)
        if len(g) < 120:
            continue
        h = g.h.to_numpy()
        l = g.l.to_numpy()
        c = g.c.to_numpy()
        mod = g["mod"].to_numpy()
        n = len(g)
        i = 121
        while i < n - M - 2:
            t0 = i
            fired = None
            for d in (+1, -1):
                if d > 0:
                    ext = l[t0]
                    if ext > l[max(0, t0 - 120):t0].min():
                        continue
                    if h[max(0, t0 - 30):t0 + 1].max() - ext < F:
                        continue
                    b_lo = l[t0 + 1:t0 + M + 1].min()
                    b_hi = h[t0 + 1:t0 + M + 1].max()
                    if b_lo < ext or (b_hi - b_lo) > r_frac * F:
                        continue
                else:
                    ext = h[t0]
                    if ext < h[max(0, t0 - 120):t0].max():
                        continue
                    if ext - l[max(0, t0 - 30):t0 + 1].min() < F:
                        continue
                    b_lo = l[t0 + 1:t0 + M + 1].min()
                    b_hi = h[t0 + 1:t0 + M + 1].max()
                    if b_hi > ext or (b_hi - b_lo) > r_frac * F:
                        continue
                fired = d
                break
            if fired is None:
                i += 1
                continue
            d = fired
            e_i = t0 + M
            entry = c[e_i]
            stop = ext - d * 1.0
            runner_tgt = entry + d * 0.5 * F
            if anchors is not None:
                hits.append((day, mod[e_i], d, entry))
            # manage
            scalp_done = False
            pA = pB = None
            eff_stop = stop
            end_i = min(e_i + 120, n - 1)
            for j in range(e_i + 1, n):
                if mod[j] >= 959 or j >= end_i:                 # MOC / time stop
                    px = c[j]
                    if pA is None:
                        pA = d * (px - entry)
                    pB = d * (px - entry)
                    break
                hit_stop = (l[j] <= eff_stop) if d > 0 else (h[j] >= eff_stop)
                hit_scalp = (h[j] >= entry + d * SCALP) if d > 0 else (l[j] <= entry - d * SCALP)
                hit_run = (h[j] >= runner_tgt) if d > 0 else (l[j] <= runner_tgt)
                if hit_stop:                                    # loss-first tie-break
                    px = eff_stop
                    if pA is None:
                        pA = d * (px - entry)
                    pB = d * (px - entry)
                    break
                if not scalp_done and hit_scalp:
                    scalp_done = True
                    pA = SCALP
                    eff_stop = entry                            # runner to breakeven
                if scalp_done and hit_run:
                    pB = d * (runner_tgt - entry)
                    break
            else:
                px = c[n - 1]
                if pA is None:
                    pA = d * (px - entry)
                pB = d * (px - entry)
            if pB is None:
                pB = 0.0 if scalp_done else pA
            pnl = (pA + pB) / 2 - COST
            trades.append(dict(day=day, month=day[:7], dir=d, pnl=pnl, entry=entry))
            i = e_i + 121                                       # one at a time + cooldown
        # end day
    T = pd.DataFrame(trades)
    return T, hits


def report(tag, T):
    if len(T) == 0:
        print(f"  {tag}: no trades")
        return
    p = T.pnl.to_numpy()
    bym = T.groupby("month").pnl.sum()
    mo = " ".join(f"{m[-2:]}:{v:+.0f}" for m, v in bym.items())
    mpos = (bym > 0).sum()
    t = p.mean() / (p.std() / np.sqrt(len(p))) if p.std() > 0 else 0
    print(f"  {tag}: n={len(p):3d} win={np.mean(p>0):.0%} tot={p.sum():+7.1f}pt "
          f"mean={p.mean():+5.2f} t={t:+4.1f} mo+={mpos}/{len(bym)} | {mo}")


D25 = load_2025()
D26 = load_2026()
print(f"2025: {D25.day.nunique()} days   2026: {D26.day.nunique()} days")

for F in (15.0, 25.0):
    for M in (10, 15):
        T25, _ = run(D25, F, M)
        T26, hits = run(D26, F, M, anchors=True)
        print(f"\n== F={F:.0f}pt flush, M={M}m base ==")
        report("2025 research", T25)
        report("2026 recorded", T26)
        anch = [x for x in hits if x[0] in ("2026-07-08", "2026-07-10") and x[2] > 0]
        if anch:
            print(f"  anchors caught: " + "; ".join(f"{a[0]} long @{a[3]:.2f}" for a in anch))
