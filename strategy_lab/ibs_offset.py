"""Can IBS's tail be cut without cutting its edge?

IBS is a crash-regime loser by construction: it buys weak closes, and in a
sustained momentum-down market it buys every one of them. The 200-day-MA filter
was already tested in QUIET_CLASSICS_SCREEN.md and HURT (+2.35 vs +3.98 pt/day)
because it removes exactly the crash REBOUNDS that pay for the losses. So the
offset has to come from somewhere other than a trend filter.

Three candidates, each tested against the unchanged baseline on the same 486
trades:

  VOL-SCALED SIZE  size ∝ 1/ATR20, normalised to the same MEAN exposure as the
                   baseline. Not a filter -- it takes every trade the baseline
                   takes, but small when a point is worth a lot of risk. The
                   worst losses all landed in high-ATR regimes, so this targets
                   the tail directly rather than guessing at direction.

  TIME STOP        exit after N sessions even if IBS never exceeds 0.80. Caps
                   how long a losing hold can compound.

  BOTH             the two combined.

Everything is causal: ATR is computed from bars strictly BEFORE the entry close.
Sizes are normalised to mean 1.0 so total P&L stays comparable -- a sizing rule
that just levers up is not an improvement, and normalising removes that trick.

    python ibs_offset.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import ibs_verify as V

COST_PT = 0.517
IN_TH, OUT_TH = 0.20, 0.80
ATR_N = 20


def atr(df: pd.DataFrame, n: int = ATR_N) -> np.ndarray:
    """Causal ATR: value at row i uses bars up to and including i, and is only
    ever consulted for an entry decided at that same close."""
    pc = df.c.shift(1)
    tr = pd.concat([df.h - df.l, (df.h - pc).abs(), (df.l - pc).abs()],
                   axis=1).max(axis=1)
    return tr.rolling(n).mean().to_numpy()


def run(df: pd.DataFrame, max_days: int | None = None,
        vol_scale: bool = False) -> pd.DataFrame:
    rng = (df.h - df.l).replace(0, np.nan)
    ibs = ((df.c - df.l) / rng).to_numpy()
    c = df.c.to_numpy()
    a = atr(df)
    idx = df.index
    out, pos, ent, ent_i = [], 0, 0.0, 0
    for i in range(len(df)):
        if np.isnan(ibs[i]):
            continue
        if pos == 0:
            if ibs[i] < IN_TH and not np.isnan(a[i]) and a[i] > 0:
                pos, ent, ent_i = 1, c[i], i
        else:
            held = i - ent_i
            if ibs[i] > OUT_TH or (max_days is not None and held >= max_days):
                out.append({"in": idx[ent_i], "out": idx[i], "days": held,
                            "pts": c[i] - ent - COST_PT, "atr": a[ent_i]})
                pos = 0
    T = pd.DataFrame(out)
    if T.empty:
        return T
    if vol_scale:
        s = 1.0 / T.atr.to_numpy()
        T["size"] = s / s.mean()          # mean exposure 1.0, same as baseline
    else:
        T["size"] = 1.0
    T["pnl"] = T.pts * T["size"]
    return T


def show(T: pd.DataFrame, label: str) -> None:
    p = T.pnl.to_numpy()
    eq = np.cumsum(p)
    dd = (eq - np.maximum.accumulate(eq)).min()
    t = p.mean() / (p.std(ddof=1) / np.sqrt(len(p)))
    print(f"  {label:<26}{len(p):>6}{p.sum():>+10,.0f}{p.mean():>+9.2f}{t:>+7.1f}"
          f"{100*(p>0).mean():>7.0f}%{p.min():>+9.0f}{dd:>+9.0f}"
          f"{p.sum()/abs(dd):>+8.2f}{T.days.mean():>7.1f}")


def main() -> None:
    df = V.spx_daily()
    print(f"\n{'='*118}")
    print(f"IBS TAIL OFFSETS — SPX daily {df.index[0]:%Y-%m}..{df.index[-1]:%Y-%m}, "
          f"net {COST_PT:.3f}pt/RT, all sizings normalised to mean exposure 1.0")
    print(f"{'='*118}")
    print(f"  {'variant':<26}{'trades':>6}{'total':>10}{'per tr':>9}{'t':>7}"
          f"{'win%':>8}{'worst':>9}{'maxDD':>9}{'ret/DD':>8}{'hold':>7}")

    base = run(df)
    show(base, "baseline (unchanged)")
    show(run(df, vol_scale=True), "vol-scaled size")
    for n in (3, 5, 10):
        show(run(df, max_days=n), f"time stop {n}d")
    for n in (5, 10):
        show(run(df, max_days=n, vol_scale=True), f"vol-scaled + {n}d stop")

    print(f"\n  ret/DD is total P&L divided by max equity drawdown — the column "
          f"that matters\n  when the question is 'same edge, smaller hole', not "
          f"'bigger number'.")
    print(f"{'='*118}\n")


if __name__ == "__main__":
    main()
