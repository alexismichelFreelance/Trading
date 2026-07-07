"""HTF-confluence study: does requiring a 30m zone to align with a higher-
timeframe (1h/4h/daily) zone improve the fade/break/flip edge?

Method: reuse the VALIDATED zones oracle detection (_detect_zones) + trade walk
(_walk). Replicate the exact FADE/BREAK/FLIP enumeration on 30m, but tag every
trade by whether its 30m zone price-band overlaps an ACTIVE (formed-before,
not-yet-broken) same-direction zone on 1h / 4h / 1d. Compare confluent vs not.
Both contracts pooled. $ P&L uses the oracle's own sizing + cost.
"""
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB
from engine.strategies.zones_oracle import (COST, K, PV, SCALP, _detect_zones,
                                            _size, _walk)

q = QuestDB(timeout=120)
SYMS = ["ESM5", "ESH5"]
HTFS = ["1h", "4h", "1d"]


def load_tf(sym, tf):
    df = q.df(f"SELECT ts, first(o) o, max(h) h, min(l) l, last(c) c, sum(vol) v "
              f"FROM claude_bars_1m WHERE symbol='{sym}' SAMPLE BY {tf} ALIGN TO CALENDAR"
              ).dropna(subset=["c"])
    ts = df["ts"].astype("int64").to_numpy()
    o, h, l, c = (df[x].to_numpy(dtype=float) for x in ("o", "h", "l", "c"))
    v = df["v"].to_numpy(dtype=float)
    return ts, o, h, l, c, v


def htf_zones(sym, tf):
    ts, o, h, l, c, v = load_tf(sym, tf)
    Z = _detect_zones(o, h, l, c, v)
    for z in Z:
        z["formed_ts"] = ts[z["j"]]
        z["broken_ts"] = None
        d, top, bot = z["dir"], z["top"], z["bot"]
        for i in range(z["j"] + 1, len(c)):
            if (c[i] < bot - 1) if d > 0 else (c[i] > top + 1):
                z["broken_ts"] = ts[i]
                break
    return Z


def confluent(zbot, ztop, d, T, HZ):
    for z in HZ:
        if z["dir"] != d or z["formed_ts"] > T:
            continue
        if z["broken_ts"] is not None and z["broken_ts"] <= T:
            continue
        if min(ztop, z["top"]) >= max(zbot, z["bot"]):     # bands intersect
            return True
    return False


trades = []          # (setup, pnl, {htf: bool}, any_bool)
for sym in SYMS:
    HZ = {tf: htf_zones(sym, tf) for tf in HTFS}
    ts, o, h, l, c, v = load_tf(sym, "30m")
    n = len(c)
    Z = _detect_zones(o, h, l, c, v)

    def tag(zbot, ztop, d, i):
        T = ts[i]
        flags = {tf: confluent(zbot, ztop, d, T, HZ[tf]) for tf in HTFS}
        return flags, any(flags.values())

    def add(setup, pnl, zbot, ztop, d, i):
        flags, anyf = tag(zbot, ztop, d, i)
        trades.append((setup, pnl, flags, anyf))

    for z in Z:
        j, d, top, bot = z["j"], z["dir"], z["top"], z["bot"]
        prox = top if d > 0 else bot
        fade_done = False
        break_bar = -1
        for i in range(j + 4, n):
            touch = (l[i] <= top and l[i] >= bot - 0.5) if d > 0 else (h[i] >= bot and h[i] <= top + 0.5)
            if touch and not fade_done:
                opp = [zz for zz in Z if zz["dir"] == -d and zz["j"] < i
                       and (zz["bot"] > prox + 3 if d > 0 else zz["top"] < prox - 3)]
                tgt = (min(zz["bot"] for zz in opp) if d > 0 else max(zz["top"] for zz in opp)) if opp \
                    else prox + d * abs(prox - (bot - 1 if d > 0 else top + 1)) * 2
                fade_stop = bot - 1 if d > 0 else top + 1
                add("FADE", _walk(d, prox, fade_stop, tgt, i, n, h, l, c), bot, top, d, i)
                fade_done = True
            if (c[i] < bot - 1) if d > 0 else (c[i] > top + 1):
                break_bar = i
                break
        if break_bar < 0 or break_bar + 2 >= n:
            continue
        bdir = -d
        b_entry = c[break_bar]
        b_stop = top + 1 if d > 0 else bot - 1
        oppB = [zz for zz in Z if zz["dir"] == d and zz["j"] < break_bar
                and (zz["bot"] > b_entry + 3 if bdir > 0 else zz["top"] < b_entry - 3)]
        b_tgt = (min(zz["bot"] for zz in oppB) if bdir > 0 else max(zz["top"] for zz in oppB)) if oppB \
            else b_entry + bdir * abs(b_entry - b_stop) * 2
        add("BREAK", _walk(bdir, b_entry, b_stop, b_tgt, break_bar + 1, n, h, l, c), bot, top, d, break_bar)
        for qb in range(break_bar + 2, n - 2):
            ft = (h[qb] >= bot and h[qb] <= top + 0.5) if d > 0 else (l[qb] <= top and l[qb] >= bot - 0.5)
            if ft:
                fdir = -d
                f_entry = bot if d > 0 else top
                f_stop = top + 1 if d > 0 else bot - 1
                oppF = [zz for zz in Z if zz["dir"] == d and zz["j"] < qb
                        and (zz["bot"] > f_entry + 3 if fdir > 0 else zz["top"] < f_entry - 3)]
                f_tgt = (min(zz["bot"] for zz in oppF) if fdir > 0 else max(zz["top"] for zz in oppF)) if oppF \
                    else f_entry + fdir * abs(f_entry - f_stop) * 2
                add("FLIP", _walk(fdir, f_entry, f_stop, f_tgt, qb, n, h, l, c), bot, top, d, qb)
                break
            if (c[qb] > top + 5) if d > 0 else (c[qb] < bot - 5):
                break


def stats(rows):
    if not rows:
        return "n=0"
    p = np.array([r[1] for r in rows])
    return f"n={len(p):3d}  win={np.mean(p>0):.0%}  tot=${p.sum():+8.0f}  mean=${p.mean():+6.0f}"


print(f"total zone trades: {len(trades)}  ({', '.join(SYMS)})\n")
for setup in ["FADE", "BREAK", "FLIP", "ALL"]:
    rows = [t for t in trades if setup == "ALL" or t[0] == setup]
    print(f"=== {setup} ===")
    print(f"  baseline      {stats(rows)}")
    print(f"  confluent-ANY {stats([t for t in rows if t[3]])}")
    print(f"  NON-confluent {stats([t for t in rows if not t[3]])}")
    for tf in HTFS:
        print(f"  conf-{tf:<3}      {stats([t for t in rows if t[2][tf]])}")
    print()
