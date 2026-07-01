"""Open-drive parity ORACLE — per-day 1-second evaluation of the open-drive
continuation (sign of the 9:30->10:00 move, held with morning-range-scaled
stops until trail/stop/16:00). Reference result on the sec-covered days
(ESM5 Apr-May + ESH5 Feb18-Mar19, 64 days): +815 pts, all 4 months positive.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from ..adapters.questdb import QuestDB
from .ignition_oracle import SEC_TABLE

COST = 0.517
STOP_MULT, TRAIL_MULT = 1.0, 1.5
STOP_FLOOR, TRAIL_FLOOR = 5.0, 8.0
RTH_OPEN_S = 9 * 3600 + 30 * 60      # 9:30 ET, seconds-of-day
ENTRY_S = 10 * 3600                  # 10:00 ET
CLOSE_S = 16 * 3600                  # 16:00 ET


def evaluate_open_drive(sym: str, q: QuestDB | None = None) -> dict[str, tuple[int, float]]:
    q = q or QuestDB()
    d = q.df(f"SELECT ts, pxc FROM {SEC_TABLE[sym]} ORDER BY ts")
    d["px"] = d["pxc"].ffill()
    ets = d["ts"].dt.tz_convert("America/New_York")
    d["sess"] = ets.dt.strftime("%Y-%m-%d")
    d["month"] = ets.dt.strftime("%Y-%m")
    d["sod"] = ets.dt.hour * 3600 + ets.dt.minute * 60 + ets.dt.second
    out: dict[str, list] = defaultdict(lambda: [0, 0.0])
    for sess, g in d.groupby("sess"):
        g = g.sort_values("sod")
        rth = g[(g.sod >= RTH_OPEN_S) & (g.sod < CLOSE_S)]
        if len(rth) < 20000:              # require a full session on the 1s grid
            continue
        p = rth["px"].to_numpy()
        sod = rth["sod"].to_numpy()
        i0 = int(np.searchsorted(sod, ENTRY_S))
        if i0 >= len(p) - 100:
            continue
        s = np.sign(p[i0] - p[0])
        if s == 0:
            continue
        m30 = p[:i0 + 1]
        rng30 = float(m30.max() - m30.min())
        stop = max(STOP_FLOOR, STOP_MULT * rng30)
        trail = max(TRAIL_FLOOR, TRAIL_MULT * rng30)
        fe = s * (p[i0 + 1:] - p[i0])
        lvl = np.maximum(-stop, np.maximum.accumulate(fe) - trail)
        hit = fe <= lvl
        pnl = (lvl[np.argmax(hit)] if hit.any() else fe[-1]) - COST
        m = g["month"].iloc[0]
        out[m][0] += 1
        out[m][1] += float(pnl)
    return {m: (v[0], v[1]) for m, v in sorted(out.items())}


__all__ = ["evaluate_open_drive"]
