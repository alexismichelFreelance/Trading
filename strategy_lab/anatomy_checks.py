"""Anatomy honesty checks:
A) withdrawal ratio DURING moves vs QUIET baseline (same nights, non-move
   seconds, both sides averaged) — is wdx=0.82 move-specific or just the book's
   unconditional churn?
B) cancel INTENSITY (receding-side cancels/sec) during moves vs quiet.
C) level enrichment for move STARTS/ENDS excluding the outcome-coupled ONH/ONL
   tags, vs random controls (also excluding ONH/ONL).
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB

q = QuestDB(timeout=180)
THETA = 8.0


def load_eth(table, cols):
    rows = []
    for d in q.df(f"SELECT DISTINCT to_str(ts,'yyyy-MM-dd') d FROM {table} ORDER BY d")["d"]:
        rows.append(q.df(f"SELECT ts, {cols} FROM {table} "
                         f"WHERE ts >= '{d}T00:00:00.000000Z' AND ts < '{d}T23:59:59.999999Z' ORDER BY ts"))
    return pd.concat(rows, ignore_index=True)


E = load_eth("claude_sec_eth", "pxc, adelta, avol")
B = load_eth("claude_sec_eth_book", "bid_add, bid_cancel, ask_add, ask_cancel")
E = E.merge(B, on="ts", how="left").fillna(0)
E["skey"] = np.where(E.ts.dt.hour >= 21,
                     (E.ts + pd.Timedelta(hours=13)).dt.strftime("%Y-%m-%d"),
                     E.ts.dt.strftime("%Y-%m-%d"))

mv_c = mv_cons = qt_c = qt_cons = 0.0
mv_secs = qt_secs = 0
starts_hit = ends_hit = ctrl_hit = 0
n_moves = n_ctrl = 0
rng = np.random.default_rng(11)

M = pd.read_csv("D:/Trading/strategy_lab/eth_move_catalog.csv")


def excl(tags):
    return [t for t in str(tags).split(",") if t not in ("-", "ONH", "ONL", "nan")]


for _, r in M.iterrows():
    if excl(r.start_tags):
        starts_hit += 1
    if excl(r.end_tags):
        ends_hit += 1
    n_moves += 1
print(f"C) level hits EXCLUDING ONH/ONL: starts {starts_hit/n_moves:.0%}  "
      f"ends {ends_hit/n_moves:.0%}  (moves n={n_moves})")

for skey, g in E.groupby("skey"):
    g = g.sort_values("ts")
    if (g.ts.max() - g.ts.min()) < pd.Timedelta(hours=8):
        continue
    grid = pd.date_range(g.ts.min().floor("s"), g.ts.max().ceil("s"), freq="1s")
    D = g.set_index("ts").reindex(grid)
    px = D["pxc"].ffill().bfill().to_numpy()
    ad = D["adelta"].fillna(0).to_numpy()
    vol = D["avol"].fillna(0).to_numpy()
    bc = D["bid_cancel"].fillna(0).to_numpy()
    ac = D["ask_cancel"].fillna(0).to_numpy()
    n = len(px)
    # re-detect legs (same zigzag as the catalog)
    hi_i = lo_i = 0
    mode = 0
    piv_i, piv_p = 0, px[0]
    legs = []
    for i in range(1, n):
        if mode == 0:
            if px[i] >= px[hi_i]:
                hi_i = i
            if px[i] <= px[lo_i]:
                lo_i = i
            if px[hi_i] - px[i] >= THETA:
                legs.append((lo_i if lo_i < hi_i else 0, hi_i, +1))
                piv_i, piv_p, mode, lo_i = hi_i, px[hi_i], -1, i
            elif px[i] - px[lo_i] >= THETA:
                legs.append((hi_i if hi_i < lo_i else 0, lo_i, -1))
                piv_i, piv_p, mode, hi_i = lo_i, px[lo_i], +1, i
            continue
        if mode == +1:
            if px[i] > px[hi_i]:
                hi_i = i
            if px[hi_i] - px[i] >= THETA:
                if px[hi_i] - piv_p >= THETA:
                    legs.append((piv_i, hi_i, +1))
                piv_i, piv_p, mode, lo_i = hi_i, px[hi_i], -1, i
        else:
            if px[i] < px[lo_i]:
                lo_i = i
            if px[i] - px[lo_i] >= THETA:
                if piv_p - px[lo_i] >= THETA:
                    legs.append((piv_i, lo_i, -1))
                piv_i, piv_p, mode, hi_i = lo_i, px[lo_i], +1, i
    inmove = np.zeros(n, bool)
    for s_i, e_i, d in legs:
        if abs(px[e_i] - px[s_i]) < THETA or e_i <= s_i:
            continue
        inmove[s_i:e_i + 1] = True
        seg = slice(s_i, e_i + 1)
        v = vol[seg].sum()
        adel = ad[seg].sum()
        cons = (v + adel) / 2 if d > 0 else (v - adel) / 2
        cans = ac[seg].sum() if d > 0 else bc[seg].sum()
        mv_c += cans
        mv_cons += cons
        mv_secs += e_i - s_i + 1
    quiet = ~inmove
    # quiet baseline: average of both sides (no direction defined)
    qcans = (bc[quiet].sum() + ac[quiet].sum()) / 2
    qv = vol[quiet].sum()
    qad = np.abs(ad[quiet]).sum()
    qcons = qv / 2                                 # symmetric: half the volume per side
    qt_c += qcans
    qt_cons += qcons
    qt_secs += int(quiet.sum())

wdx_mv = mv_c / (mv_c + mv_cons)
wdx_qt = qt_c / (qt_c + qt_cons)
print(f"\nA) withdrawal ratio: MOVES {wdx_mv:.2f}   QUIET baseline {wdx_qt:.2f}")
print(f"B) receding-side cancel intensity: MOVES {mv_c/max(1,mv_secs):.0f}/s   "
      f"QUIET {qt_c/max(1,qt_secs):.0f}/s   "
      f"consumption: MOVES {mv_cons/max(1,mv_secs):.0f}/s  QUIET {qt_cons/max(1,qt_secs):.0f}/s")
