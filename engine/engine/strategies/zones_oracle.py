"""Zone-lifecycle parity ORACLE (engine_reference/zone_lifecycle_reference.md).

Three setups per detected 30m S/D zone: FADE (fresh-zone 1st-touch scale-out),
BREAK (trade WITH a failed zone), FLIP (broken zone retested from the broken
side, trade the flipped polarity). Sized 2%/$2k, half off at +4 with a breakeven
runner to the opposing zone. Look-ahead-safe: target zones must have formed
before the trade (o.j < currentBar).

Parity ($, sized): FADE +17,534 / BREAK +5,180 / FLIP +24,789 / COMBINED +47,502.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np
import pandas as pd

from ..adapters.questdb import QuestDB
from .ignition_oracle import SEC_TABLE

PV = 50.0
RISK = 2000.0
MAXC = 30
SCALP = 4.0
K = 16          # max bars held
COST = 0.5      # $/contract round-turn (per reference)


def _size(risk_pts: float) -> int:
    return max(1, min(MAXC, int(RISK / (max(1.0, risk_pts) * PV))))


def _month(ns: int) -> str:
    return pd.Timestamp(ns, unit="ns", tz="UTC").strftime("%Y-%m")


def _detect_zones(o, h, l, c, v):
    n = len(c)
    rng = h - l
    zones = []
    for j in range(4, n - 5):
        aR = rng[j - 20:j].mean() if j >= 20 else rng[max(0, j - 20):j].mean()
        aV = v[j - 20:j].mean() if j >= 20 else v[max(0, j - 20):j].mean()
        if not (rng[j] >= 1.4 * aR and abs(c[j] - o[j]) >= 0.5 * rng[j] and v[j] >= aV):
            continue
        d = 1 if c[j] > o[j] else -1
        base = []
        for b in (j - 1, j - 2, j - 3):
            if b >= 0 and rng[b] <= 0.8 * aR:
                base.append(b)
            else:
                break
        if not base:
            continue
        top = max(h[b] for b in base)
        bot = min(l[b] for b in base)
        if top - bot < 0.5:
            continue
        zones.append({"j": j, "dir": d, "top": top, "bot": bot})
    return zones


def _walk(direction, entry, stop, target, si, n, h, l, c):
    risk = abs(entry - stop)
    size = _size(risk)
    sc = entry + direction * SCALP
    scH = stH = tgH = -1
    for k in range(si, min(si + K, n)):
        if direction > 0:
            if stH < 0 and l[k] <= stop:
                stH = k
            if scH < 0 and h[k] >= sc:
                scH = k
            if tgH < 0 and h[k] >= target:
                tgH = k
        else:
            if stH < 0 and h[k] >= stop:
                stH = k
            if scH < 0 and l[k] <= sc:
                scH = k
            if tgH < 0 and l[k] <= target:
                tgH = k
        if stH >= 0 and (scH < 0 or stH < scH):
            break
        if tgH >= 0:
            break
    if stH >= 0 and (scH < 0 or stH < scH):
        pA = pB = -risk
    elif scH >= 0:
        pA = SCALP
        pB = abs(target - entry) if (tgH >= 0 and tgH >= scH) else 0.0
    else:
        last = c[min(si + K - 1, n - 1)]
        pA = pB = direction * (last - entry)
    return ((pA + pB) / 2.0) * PV * size - COST * size


def evaluate_zones(sym: str, q: QuestDB | None = None):
    q = q or QuestDB()
    df = q.df(f"SELECT ts, first(o) o, max(h) h, min(l) l, last(c) c, sum(vol) v "
              f"FROM claude_bars_1m WHERE symbol='{sym}' SAMPLE BY 30m ALIGN TO CALENDAR").dropna(subset=["c"])
    ts = df["ts"].astype("int64").to_numpy()
    o, h, l, c = (df[x].to_numpy(dtype=float) for x in ("o", "h", "l", "c"))
    v = df["v"].to_numpy(dtype=float)
    n = len(df)
    Z = _detect_zones(o, h, l, c, v)

    setups = defaultdict(lambda: [0, 0.0])       # name -> [n, $]
    months = defaultdict(lambda: [0, 0.0])        # month -> [n, $]

    def emit(name, pnl, bar):
        setups[name][0] += 1
        setups[name][1] += pnl
        months[_month(ts[bar])][0] += 1
        months[_month(ts[bar])][1] += pnl

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
                if opp:
                    tgt = min(zz["bot"] for zz in opp) if d > 0 else max(zz["top"] for zz in opp)
                else:
                    tgt = prox + d * abs(prox - (bot - 1 if d > 0 else top + 1)) * 2
                fade_stop = bot - 1 if d > 0 else top + 1
                emit("FADE", _walk(d, prox, fade_stop, tgt, i, n, h, l, c), i)
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
        if oppB:
            b_tgt = min(zz["bot"] for zz in oppB) if bdir > 0 else max(zz["top"] for zz in oppB)
        else:
            b_tgt = b_entry + bdir * abs(b_entry - b_stop) * 2
        emit("BREAK", _walk(bdir, b_entry, b_stop, b_tgt, break_bar + 1, n, h, l, c), break_bar)

        for qb in range(break_bar + 2, n - 2):
            flip_touch = (h[qb] >= bot and h[qb] <= top + 0.5) if d > 0 else (l[qb] <= top and l[qb] >= bot - 0.5)
            if flip_touch:
                fdir = -d
                f_entry = bot if d > 0 else top
                f_stop = top + 1 if d > 0 else bot - 1
                oppF = [zz for zz in Z if zz["dir"] == d and zz["j"] < qb
                        and (zz["bot"] > f_entry + 3 if fdir > 0 else zz["top"] < f_entry - 3)]
                if oppF:
                    f_tgt = min(zz["bot"] for zz in oppF) if fdir > 0 else max(zz["top"] for zz in oppF)
                else:
                    f_tgt = f_entry + fdir * abs(f_entry - f_stop) * 2
                emit("FLIP", _walk(fdir, f_entry, f_stop, f_tgt, qb, n, h, l, c), qb)
                break
            if (c[qb] > top + 5) if d > 0 else (c[qb] < bot - 5):
                break

    return {"setups": {k: tuple(vv) for k, vv in setups.items()},
            "months": {k: tuple(vv) for k, vv in sorted(months.items())}}


__all__ = ["evaluate_zones"]
