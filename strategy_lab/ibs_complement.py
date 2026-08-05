"""What makes money exactly when IBS bleeds?

Not a filter. IBS's tail cannot be filtered off -- vol-scaling, the 200dMA
filter and gamma sizing were all tested and all cut the edge with the tail
(ibs_offset.py). So the answer has to be a SECOND position that earns in the
windows where IBS is losing.

IBS is long-only daily mean reversion: it buys weak closes and holds ~5 days. It
therefore bleeds in exactly one situation -- a sustained downtrend, where every
weak close is followed by a weaker one. The candidates below are all things that
should, mechanically, earn there. Whether they actually do is the question.

Method:
  1. IBS daily P&L series, position x next-day change, with the 0.90 exit.
  2. IBS BAD DAYS = the days IBS is in a position AND losing money. That is the
     window to be covered -- not calendar drawdowns, which include flat periods
     where nothing needs offsetting.
  3. Each candidate scored on (a) its return on those days specifically, and
     (b) whether IBS+candidate has better return/drawdown than IBS alone. (b) is
     the only thing that matters -- a hedge that pays in the bad window but
     bleeds the rest of the year is not a complement, it is a cost.

All candidates are causal: position for day t is decided from data through t-1.

    python ibs_complement.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import ibs_verify as V

COST_PT = 0.517
IN_TH, OUT_TH = 0.20, 0.90


def ibs_positions(df: pd.DataFrame) -> np.ndarray:
    """1 while the sleeve holds, 0 otherwise. Decided at the close of day t, so
    it earns the move from t to t+1."""
    rng = (df.h - df.l).replace(0, np.nan)
    ibs = ((df.c - df.l) / rng).to_numpy()
    pos = np.zeros(len(df))
    holding = False
    for i in range(len(df)):
        if np.isnan(ibs[i]):
            pos[i] = 1.0 if holding else 0.0
            continue
        if not holding and ibs[i] < IN_TH:
            holding = True
        elif holding and ibs[i] > OUT_TH:
            holding = False
        pos[i] = 1.0 if holding else 0.0
    return pos


def candidates(df: pd.DataFrame) -> dict[str, np.ndarray]:
    """name -> position series (decided at close of t, earns t -> t+1)."""
    c = df.c
    ma50, ma200 = c.rolling(50).mean(), c.rolling(200).mean()
    hi20, lo20 = c.rolling(20).max(), c.rolling(20).min()
    hi55, lo55 = c.rolling(55).max(), c.rolling(55).min()
    rng = (df.h - df.l).replace(0, np.nan)
    ibs = (df.c - df.l) / rng
    ret = c.diff()
    vol20 = ret.rolling(20).std()

    out = {
        # trend-following: the classic answer to "mean reversion is bleeding"
        "TSMOM 200d L/S": np.sign(c - ma200),
        "short < 200dMA": -(c < ma200).astype(float),
        "short < 50dMA": -(c < ma50).astype(float),
        # Donchian: short new lows, the thing that IS happening when IBS bleeds
        "Donchian20 short": -(c <= lo20).astype(float),
        "Donchian55 short": -(c <= lo55).astype(float),
        "Donchian20 L/S": ((c >= hi20).astype(float) - (c <= lo20).astype(float)),
        # the mirror of IBS itself: sell strong closes
        "inverse IBS": -(ibs > 0.80).astype(float),
        # vol expansion: sit short while realised vol is rising
        "short vol-spike": -((vol20 > vol20.rolling(60).mean() * 1.5)
                             & (c < ma50)).astype(float),
    }
    return {k: pd.Series(v, index=df.index).shift(0).fillna(0).to_numpy()
            for k, v in out.items()}


def pnl(pos: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Daily P&L in points, charging cost on every position CHANGE."""
    d = np.diff(c, prepend=c[0])
    p = np.zeros(len(c))
    p[1:] = pos[:-1] * d[1:]
    turn = np.abs(np.diff(pos, prepend=0.0))
    return p - turn * COST_PT


def stats(p: np.ndarray) -> tuple[float, float, float]:
    eq = np.cumsum(p)
    dd = (eq - np.maximum.accumulate(eq)).min()
    return p.sum(), dd, (p.sum() / abs(dd) if dd < 0 else np.inf)


def main() -> None:
    df = V.spx_daily()
    c = df.c.to_numpy()
    ipos = ibs_positions(df)
    ip = pnl(ipos, c)
    bad = (ipos_shift := np.roll(ipos, 1)) > 0
    bad[0] = False
    bad &= ip < 0                       # in a position AND losing

    it, idd, ir = stats(ip)
    print(f"\n{'='*104}")
    print(f"IBS COMPLEMENT — SPX daily {df.index[0]:%Y-%m}..{df.index[-1]:%Y-%m}, "
          f"net {COST_PT}pt per position change")
    print(f"{'='*104}")
    print(f"  IBS alone: {it:+,.0f}pt   maxDD {idd:+,.0f}   ret/DD {ir:+.2f}   "
          f"exposure {100*ipos.mean():.0f}%")
    print(f"  BAD WINDOW = {int(bad.sum())} days IBS held and lost, "
          f"totalling {ip[bad].sum():+,.0f}pt\n")

    print(f"  {'candidate':<20}{'own total':>11}{'own ret/DD':>12}"
          f"{'IN BAD WINDOW':>15}{'corr':>7}   {'COMBINED':>10}{'ret/DD':>9}"
          f"{'vs IBS':>9}")
    rows = []
    for name, pos in candidates(df).items():
        p = pnl(pos, c)
        ot, odd, orr = stats(p)
        inbad = p[bad].sum()
        cr = np.corrcoef(ip, p)[0, 1]
        ct, cdd, crr = stats(ip + p)
        rows.append((crr, name, ot, orr, inbad, cr, ct, crr, crr - ir))
    for _, name, ot, orr, inbad, cr, ct, crr, delta in sorted(rows, reverse=True):
        flag = "  <-- better" if delta > 0 else ""
        print(f"  {name:<20}{ot:>+11,.0f}{orr:>+12.2f}{inbad:>+15,.0f}"
              f"{cr:>+7.2f}   {ct:>+10,.0f}{crr:>+9.2f}{delta:>+9.2f}{flag}")

    print(f"\n  IN BAD WINDOW = the candidate's P&L on the {int(bad.sum())} days "
          f"IBS was losing.\n  COMBINED = IBS + candidate, one unit each. The "
          f"last column is the only\n  test that counts: does adding it improve "
          f"IBS's return per unit of drawdown?")
    print(f"{'='*104}\n")


if __name__ == "__main__":
    main()
