"""Flow-following parity ORACLE (engine_reference/flow_following_reference.md).

Continuous-tape strategy: hold a position proportional to a THRESHOLDED windowed
aggressor-flow signal, with an ASYMMETRIC hold band (add-band a=1 small, hold/
flip-band h=5 wide -> easy to add, hard to reverse). Flat reset each session.
Parity target: +814 pts (unsized, net of 0.30/turnover), all months positive.

Flow is inherently single-position, so this per-day algorithm IS the strategy
logic; the live FlowFollowingStrategy emits the same position adjustments.
"""
from __future__ import annotations

from collections import defaultdict, deque

import numpy as np
import pandas as pd

from ..adapters.questdb import QuestDB
from .ignition_oracle import SEC_TABLE

# the +814 config
W, TH, SCALE, MAXP = 120, 200, 3000, 50
ADD_BAND, HOLD_BAND = 1, 5
COST = 0.30


def _jsround(x: float) -> int:
    return int(np.floor(x + 0.5))          # JS Math.round (half up), matches reference


def _sign(x: float) -> int:
    return int(x > 0) - int(x < 0)


def evaluate_flow(sym: str, q: QuestDB | None = None,
                  w: int = W, th: int = TH, scale: float = SCALE, maxp: int = MAXP,
                  a: int = ADD_BAND, h: int = HOLD_BAND, cost: float = COST
                  ) -> dict[str, tuple[float, float]]:
    """Return {month: (net_pts, turnover)} for the per-second flow strategy."""
    q = q or QuestDB()
    d = q.df(f"SELECT ts, day, pxc, adelta FROM {SEC_TABLE[sym]} ORDER BY ts")
    d["px"] = d["pxc"].ffill()
    d["daykey"] = d["day"].dt.strftime("%Y-%m-%d")
    d["month"] = d["ts"].dt.strftime("%Y-%m")
    acc: dict[str, list] = defaultdict(lambda: [0.0, 0.0])   # month -> [pnl, turn]

    for _, g in d.groupby("daykey"):
        p = g["px"].to_numpy()
        dd = g["adelta"].to_numpy()
        month = g["month"].iloc[0]
        n = len(g)
        F = 0.0
        buf: deque[float] = deque()
        held = 0
        pnl = 0.0
        turn = 0.0
        for i in range(n):
            x = dd[i] if abs(dd[i]) >= th else 0
            buf.append(x)
            F += x
            if len(buf) > w:
                F -= buf.popleft()
            tgt = max(-maxp, min(maxp, F / scale))
            delta = tgt - held
            band = a if held == 0 else (a if _sign(delta) == _sign(held) else h)
            if abs(delta) > band:
                nv = _jsround(tgt)
                turn += abs(nv - held)
                held = nv
            if i < n - 1:
                pnl += held * (p[i + 1] - p[i])
        acc[month][0] += pnl
        acc[month][1] += turn

    return {m: (pnl - turn * cost, turn) for m, (pnl, turn) in sorted(acc.items())}


__all__ = ["evaluate_flow"]
