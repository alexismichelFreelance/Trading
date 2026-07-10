"""ROUND 2 — VWAP-band / prior-level retest fade (the user's decoded trade).

LONG (shorts mirrored on +bands): during RTH, after an intraday decline >= F pts
within the last 90m and >=10m since the local low, buy the FIRST touch of:
   setup A: VWAP - 2*sigma
   setup B: prior-day close, if it sits within 3pt of VWAP-1s or -2s (confluence)
Stop = trigger level - 6 (fixed structural buffer, ~their flush-low risk).
Scalp half +4 -> runner to VWAP (recomputed at entry), stop to BE after scalp.
Time stop 120m, MOC 15:59. Cost 0.517. One position at a time, 60m cooldown.
Anchor test: must catch 2026-07-08 ~7492 and 2026-07-10 ~7586 longs.
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB

q = QuestDB(timeout=180)
COST = 0.517
SCALP = 4.0
STOPB = 6.0


def load_2025():
    df = q.df("SELECT symbol, ts, o, h, l, c, vol FROM claude_bars_1m ORDER BY ts")
    et = df.ts.dt.tz_convert("America/New_York")
    df["day"] = et.dt.strftime("%Y-%m-%d")
    df["mod"] = et.dt.hour * 60 + et.dt.minute
    df = df[(df["mod"] >= 570) & (df["mod"] < 960)]
    df = df[~((df.symbol == "ESH5") & (df.day >= "2025-03-20"))]
    df = df[~((df.symbol == "ESM5") & (df.day < "2025-03-20"))]
    return df


def load_2026():
    df = q.df("SELECT ts, o, h, l, c, vol FROM claude_bars_live ORDER BY ts")
    et = df.ts.dt.tz_convert("America/New_York")
    df["day"] = et.dt.strftime("%Y-%m-%d")
    df["mod"] = et.dt.hour * 60 + et.dt.minute
    return df[(df["mod"] >= 570) & (df["mod"] < 960)]


def run(df, F=15.0, conf=3.0, anchors=False):
    trades, hits = [], []
    prev_close = {}
    days_sorted = sorted(df.day.unique())
    for i, d in enumerate(days_sorted):
        if i > 0:
            g0 = df[df.day == days_sorted[i - 1]]
            prev_close[d] = g0.c.iloc[-1]
    for day, g in df.groupby("day"):
        g = g.sort_values("mod").reset_index(drop=True)
        if len(g) < 200:
            continue
        h = g.h.to_numpy(); l = g.l.to_numpy(); c = g.c.to_numpy()
        vol = g.vol.to_numpy().astype(float); mod = g["mod"].to_numpy()
        cv = np.cumsum(vol)
        cpv = np.cumsum(c * vol)
        vwap = np.where(cv > 0, cpv / np.maximum(cv, 1), c)
        cd2 = np.cumsum(vol * (c - vwap) ** 2)
        sd = np.sqrt(np.where(cv > 0, cd2 / np.maximum(cv, 1), 0.0))
        pc = prev_close.get(day)
        n = len(g)
        i = 40
        while i < n - 5:
            # flush precondition (long side): decline >= F in last 90m, low >=10m ago
            w0 = max(0, i - 90)
            hh = h[w0:i + 1].max()
            ll = l[w0:i + 1].min()
            li = w0 + int(np.argmin(l[w0:i + 1]))
            long_ok = (hh - ll >= F) and (i - li >= 10) and c[i] > ll
            lo_hh_i = w0 + int(np.argmax(h[w0:i + 1]))
            short_ok = (hh - ll >= F) and (i - lo_hh_i >= 10) and c[i] < hh
            fired = None
            for dset, dsgn in (("A", +1), ("B", +1), ("A", -1), ("B", -1)):
                if dsgn > 0 and not long_ok:
                    continue
                if dsgn < 0 and not short_ok:
                    continue
                if dset == "A":
                    lvl = vwap[i] - dsgn * 2 * sd[i]
                else:
                    if pc is None:
                        continue
                    band1 = vwap[i] - dsgn * sd[i]
                    band2 = vwap[i] - dsgn * 2 * sd[i]
                    if min(abs(pc - band1), abs(pc - band2)) > conf:
                        continue
                    lvl = pc
                # first touch this minute from the right side
                touched = (l[i] <= lvl <= h[i])
                prior_clear = (l[max(0, i - 15):i] > lvl).all() if dsgn > 0 else \
                              (h[max(0, i - 15):i] < lvl).all()
                if touched and prior_clear:
                    fired = (dsgn, dset, lvl)
                    break
            if fired is None:
                i += 1
                continue
            dsgn, dset, lvl = fired
            entry = lvl
            stop = entry - dsgn * STOPB
            runner_tgt = vwap[i]
            if anchors:
                hits.append((day, int(mod[i]), dsgn, round(entry, 2), dset))
            scalp_done = False
            pA = pB = None
            eff = stop
            end_i = min(i + 120, n - 1)
            for j in range(i + 1, n):
                if mod[j] >= 959 or j >= end_i:
                    px = c[j]
                    if pA is None:
                        pA = dsgn * (px - entry)
                    pB = dsgn * (px - entry)
                    break
                if (l[j] <= eff) if dsgn > 0 else (h[j] >= eff):
                    px = eff
                    if pA is None:
                        pA = dsgn * (px - entry)
                    pB = dsgn * (px - entry)
                    break
                if not scalp_done and ((h[j] >= entry + dsgn * SCALP) if dsgn > 0
                                       else (l[j] <= entry + dsgn * SCALP)):
                    scalp_done = True
                    pA = SCALP
                    eff = entry
                if scalp_done and ((h[j] >= runner_tgt) if dsgn > 0 else (l[j] <= runner_tgt)):
                    pB = dsgn * (runner_tgt - entry)
                    break
            else:
                px = c[n - 1]
                if pA is None:
                    pA = dsgn * (px - entry)
                pB = dsgn * (px - entry)
            if pB is None:
                pB = 0.0
            trades.append(dict(day=day, month=day[:7], dir=dsgn, setup=dset,
                               pnl=(pA + pB) / 2 - COST))
            i += 60
    return pd.DataFrame(trades), hits


def report(tag, T):
    if len(T) == 0:
        print(f"  {tag}: no trades")
        return
    p = T.pnl.to_numpy()
    bym = T.groupby("month").pnl.sum()
    mo = " ".join(f"{m[-2:]}:{v:+.0f}" for m, v in bym.items())
    t = p.mean() / (p.std() / np.sqrt(len(p))) if p.std() > 0 and len(p) > 2 else 0
    for ds in ("A", "B"):
        sub = T[T.setup == ds]
        if len(sub):
            print(f"    setup {ds}: n={len(sub):3d} win={np.mean(sub.pnl>0):.0%} "
                  f"tot={sub.pnl.sum():+7.1f}pt mean={sub.pnl.mean():+5.2f}")
    print(f"  {tag}: n={len(p):3d} win={np.mean(p>0):.0%} tot={p.sum():+7.1f}pt "
          f"mean={p.mean():+5.2f} t={t:+4.1f} mo+={(bym>0).sum()}/{len(bym)} | {mo}")


D25 = load_2025()
D26 = load_2026()
T25, _ = run(D25)
T26, hits = run(D26, anchors=True)
print("== VWAP-band / prior-close retest fade ==")
report("2025 research", T25)
report("2026 recorded", T26)
anch = [x for x in hits if x[0] in ("2026-07-08", "2026-07-10") and x[2] > 0]
print("\nanchor check (need ~7492 on 07-08, ~7586 on 07-10):")
for a in hits:
    if a[0] in ("2026-07-08", "2026-07-10"):
        print(f"  {a[0]} {'L' if a[2]>0 else 'S'} @{a[3]} ({a[4]}) at {a[1]//60:02d}:{a[1]%60:02d} ET")
print("\nlongs vs shorts, 2026:", T26.groupby("dir").pnl.agg(["count", "sum"]).to_dict())
