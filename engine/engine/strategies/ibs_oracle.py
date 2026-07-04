"""IBS swing parity ORACLE — daily-bar backtest of the IBS mean-reversion rule.

Rule (classic, no stop): BUY at close when IBS = (C-L)/(H-L) < in_th while
flat; SELL at close when IBS > out_th. MOC execution both sides; cost charged
per round-turn.

Parity target (fixed window so data refreshes don't drift it):
ES=F continuous daily, 2010-01-04..2026-06-30, in 0.20 / out 0.80, cost 0.517:
n=435 trades, total +4644.0 pts (verified 2026-07; whole 3x3 parameter plateau
t=4.1-5.0, H2>H1, SPY confirms at t=4.4).

Known risk profile (documented, intrinsic to no-stop MR): worst trade -347pt,
worst MAE -601pt, 1-lot closed-equity maxDD -$24.5k. GEX conditioning: entries
in LONG-gamma regimes win 78% @ +14.9/trade (worst -113) vs SHORT-gamma 66% @
+7.5 (worst -347) — sizing lever, see gamma/GEX_FINDINGS.md.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

COST = 0.517
IN_TH = 0.20
OUT_TH = 0.80


def evaluate_ibs(df: pd.DataFrame, in_th: float = IN_TH, out_th: float = OUT_TH,
                 cost: float = COST, end_date: str | None = None) -> pd.DataFrame:
    """df: columns date,o,h,l,c (daily). Returns one row per closed trade."""
    if end_date:
        df = df[df.date <= end_date]
    df = df[df.h > df.l].reset_index(drop=True)
    c = df["c"].to_numpy(); h = df["h"].to_numpy(); l = df["l"].to_numpy()
    ibs = (c - l) / (h - l)
    out = []
    i, n = 0, len(df)
    while i < n - 1:
        if ibs[i] < in_th:
            entry, e_i = c[i], i
            j = i + 1
            mae = 0.0
            while j < n - 1 and ibs[j] <= out_th:
                mae = min(mae, l[j] - entry)
                j += 1
            mae = min(mae, l[j] - entry)
            out.append(dict(entry_date=df.at[e_i, "date"], exit_date=df.at[j, "date"],
                            pnl=c[j] - entry - cost, hold=j - e_i, mae=mae))
            i = j + 1
        else:
            i += 1
    return pd.DataFrame(out)


def load_daily_csv(path: str | Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype={"date": str})


def summary(T: pd.DataFrame) -> dict:
    p = T["pnl"].to_numpy()
    eq = np.cumsum(p)
    return dict(n=len(T), total=float(p.sum()), mean=float(p.mean()),
                tstat=float(p.mean() / (p.std() / np.sqrt(len(p)))),
                win=float((p > 0).mean()),
                worst=float(p.min()),
                maxdd=float((eq - np.maximum.accumulate(eq)).min()))


__all__ = ["evaluate_ibs", "load_daily_csv", "summary", "COST", "IN_TH", "OUT_TH"]
