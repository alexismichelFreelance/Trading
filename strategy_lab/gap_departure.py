"""Gaps-as-departures study: does treating the overnight/RTH-open gap as a
departure (a fresh zone) add edge to the zones sleeve?

Method: on RTH 30m bars (claude_bars_1m), keep the validated base->departure
zones AND add a GAP zone at each session open (gap = first-bar open - prior
session's last close; gap-up -> demand [prev_close,open], gap-down -> supply).
Run the SAME oracle fade/break/flip lifecycle + trade walk (_walk) on the
combined zone set, tag each trade by origin (base vs gap), compare. Threshold
sweep on |gap|. Both contracts. Baseline base-only reproduces the parity.
"""
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB
from engine.strategies.zones_oracle import (COST, K, PV, SCALP, _detect_zones,
                                            _size, _walk)

q = QuestDB(timeout=120)
SYMS = ["ESM5", "ESH5"]
GAP_THRESHOLDS = [3.0, 5.0, 8.0]


def load(sym):
    df = q.df(f"SELECT ts, first(o) o, max(h) h, min(l) l, last(c) c, sum(vol) v "
              f"FROM claude_bars_1m WHERE symbol='{sym}' SAMPLE BY 30m ALIGN TO CALENDAR"
              ).dropna(subset=["c"])
    et = df["ts"].dt.tz_convert("America/New_York")
    df["sess"] = et.dt.strftime("%Y-%m-%d")
    return df.reset_index(drop=True)


def gap_zones(df, thr):
    """One zone per session where |open - prior_session_close| >= thr."""
    o = df["o"].to_numpy(float); c = df["c"].to_numpy(float)
    sess = df["sess"].to_numpy()
    first_idx, last_idx = {}, {}
    for i, s in enumerate(sess):
        if s not in first_idx:
            first_idx[s] = i
        last_idx[s] = i
    order = list(dict.fromkeys(sess))
    zones = []
    for k in range(1, len(order)):
        oi = first_idx[order[k]]
        pc = c[last_idx[order[k - 1]]]
        op = o[oi]
        gap = op - pc
        if abs(gap) < thr:
            continue
        d = 1 if gap > 0 else -1                     # gap up -> demand, down -> supply
        zones.append({"j": oi, "dir": d, "top": max(pc, op), "bot": min(pc, op),
                      "origin": "gap"})
    return zones


def enumerate_trades(o, h, l, c, v, Ztrade, Ztgt, off):
    """Oracle fade/break/flip over Ztrade, targeting opposing zones in Ztgt,
    fade/break scan starting at j+off. yields (setup, pnl, origin). With
    Ztrade=Ztgt=base and off=4 this reproduces the parity oracle exactly."""
    n = len(c)
    out = []
    Z = Ztgt
    for z in Ztrade:
        j, d, top, bot = z["j"], z["dir"], z["top"], z["bot"]
        og = z.get("origin", "base")
        prox = top if d > 0 else bot
        fade_done = False
        break_bar = -1
        for i in range(j + off, n):
            touch = (l[i] <= top and l[i] >= bot - 0.5) if d > 0 else (h[i] >= bot and h[i] <= top + 0.5)
            if touch and not fade_done:
                opp = [zz for zz in Z if zz["dir"] == -d and zz["j"] < i
                       and (zz["bot"] > prox + 3 if d > 0 else zz["top"] < prox - 3)]
                tgt = (min(zz["bot"] for zz in opp) if d > 0 else max(zz["top"] for zz in opp)) if opp \
                    else prox + d * abs(prox - (bot - 1 if d > 0 else top + 1)) * 2
                fs = bot - 1 if d > 0 else top + 1
                out.append(("FADE", _walk(d, prox, fs, tgt, i, n, h, l, c), og))
                fade_done = True
            if (c[i] < bot - 1) if d > 0 else (c[i] > top + 1):
                break_bar = i
                break
        if break_bar < 0 or break_bar + 2 >= n:
            continue
        bdir = -d
        be = c[break_bar]
        bs = top + 1 if d > 0 else bot - 1
        oppB = [zz for zz in Z if zz["dir"] == d and zz["j"] < break_bar
                and (zz["bot"] > be + 3 if bdir > 0 else zz["top"] < be - 3)]
        bt = (min(zz["bot"] for zz in oppB) if bdir > 0 else max(zz["top"] for zz in oppB)) if oppB \
            else be + bdir * abs(be - bs) * 2
        out.append(("BREAK", _walk(bdir, be, bs, bt, break_bar + 1, n, h, l, c), og))
        for qb in range(break_bar + 2, n - 2):
            ft = (h[qb] >= bot and h[qb] <= top + 0.5) if d > 0 else (l[qb] <= top and l[qb] >= bot - 0.5)
            if ft:
                fdir = -d
                fe = bot if d > 0 else top
                fst = top + 1 if d > 0 else bot - 1
                oppF = [zz for zz in Z if zz["dir"] == d and zz["j"] < qb
                        and (zz["bot"] > fe + 3 if fdir > 0 else zz["top"] < fe - 3)]
                ft2 = (min(zz["bot"] for zz in oppF) if fdir > 0 else max(zz["top"] for zz in oppF)) if oppF \
                    else fe + fdir * abs(fe - fst) * 2
                out.append(("FLIP", _walk(fdir, fe, fst, ft2, qb, n, h, l, c), og))
                break
            if (c[qb] > top + 5) if d > 0 else (c[qb] < bot - 5):
                break
    return out


def stat(rows):
    if not rows:
        return "n=0"
    p = np.array([r[1] for r in rows])
    return f"n={len(p):3d}  win={np.mean(p>0):.0%}  tot=${p.sum():+8.0f}  mean=${p.mean():+6.0f}"


# base baseline (isolated, must reproduce parity 77 / +$47,502)
base_all = []
data = {}
for sym in SYMS:
    df = load(sym)
    o, h, l, c = (df[x].to_numpy(float) for x in ("o", "h", "l", "c"))
    v = df["v"].to_numpy(float)
    Zb = _detect_zones(o, h, l, c, v)
    for z in Zb:
        z["origin"] = "base"
    data[sym] = (df, o, h, l, c, v, Zb)
    base_all += enumerate_trades(o, h, l, c, v, Zb, Zb, 4)
print(f"BASE baseline (isolated): {stat(base_all)}   [parity target: n=77 $+47,502]")

for thr in GAP_THRESHOLDS:
    gap_all = []
    n_gapz = 0
    for sym in SYMS:
        df, o, h, l, c, v, Zb = data[sym]
        Zg = gap_zones(df, thr)
        n_gapz += len(Zg)
        # gap zones evaluated as an ADD-ON: target the real (base) structure,
        # fade/break can trigger 1 bar after the open (gap is instant, no base)
        gap_all += enumerate_trades(o, h, l, c, v, Zg, Zb, 1)
    print(f"\n===== gap threshold >= {thr}pt   ({n_gapz} gap zones) =====")
    print(f"  GAP zones (add-on)   {stat(gap_all)}")
    for setup in ("FADE", "BREAK", "FLIP"):
        print(f"     {setup:<5}            {stat([t for t in gap_all if t[0]==setup])}")
    print(f"  base+gap COMBINED    {stat(base_all + gap_all)}")
