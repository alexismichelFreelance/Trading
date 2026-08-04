"""Exits, tested properly: same 486 entries, twelve ways out, 16 years.

Every exit argument so far has been made on 20-70 sessions, which is why they
all inverted. This holds the ENTRY fixed (IBS < 0.20 at the close, the one rule
in this project with t=5.0 behind it) and varies only the exit, so the
comparison is paired -- differences are attributable to the exit and nothing
else.

The question is the one that matters: can anything tell you the move is DONE
better than the incumbent rule (IBS > 0.80)?

Exits tested:
  thesis      IBS > 0.80                        the incumbent
  thesis_X    IBS > X                           is the threshold even right?
  target_N    +N x ATR20 from entry             fixed profit target
  trail_N     N x ATR20 off the running high    give back N ATR
  first_up    first close above entry           take anything green
  pdh         close above the prior session high
  time_N      after N sessions regardless
  thesis+time incumbent, capped at 10 sessions
  bh_N        hold exactly N sessions (control)

Reported per trade NET of 0.517pt, with t, and with return/maxDD -- because an
exit that makes more by holding through deeper holes is not obviously better.

    python ibs_exits.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import ibs_verify as V

COST = 0.517
IN_TH = 0.20


def atr20(df: pd.DataFrame) -> np.ndarray:
    pc = df.c.shift(1)
    tr = pd.concat([df.h - df.l, (df.h - pc).abs(), (df.l - pc).abs()],
                   axis=1).max(axis=1)
    return tr.rolling(20).mean().to_numpy()


def entries(df: pd.DataFrame, ibs: np.ndarray, a: np.ndarray) -> list[int]:
    """Every IBS<0.20 close that is not already inside a position is skipped by
    the walk below; here we just list candidate bars once, in order."""
    return [i for i in range(len(df))
            if np.isfinite(ibs[i]) and ibs[i] < IN_TH and np.isfinite(a[i])
            and a[i] > 0]


def run(df: pd.DataFrame, rule) -> pd.DataFrame:
    """rule(i, j, state) -> True to exit at the close of bar j (j > i)."""
    ibs_ = ((df.c - df.l) / (df.h - df.l).replace(0, np.nan)).to_numpy()
    c, h, l = df.c.to_numpy(), df.h.to_numpy(), df.l.to_numpy()
    a = atr20(df)
    n = len(df)
    out, i = [], 0
    cand = set(entries(df, ibs_, a))
    while i < n - 1:
        if i not in cand:
            i += 1
            continue
        ent, peak = c[i], c[i]
        j = i + 1
        while j < n:
            peak = max(peak, c[j])
            if rule(i, j, dict(ent=ent, peak=peak, ibs=ibs_, c=c, h=h, l=l,
                               atr=a[i], df=df)):
                break
            j += 1
        j = min(j, n - 1)
        out.append({"in": df.index[i], "out": df.index[j], "days": j - i,
                    "pts": c[j] - ent - COST})
        i = j + 1                      # flat before the next entry, as the sleeve is
    return pd.DataFrame(out)


def show(T: pd.DataFrame, label: str) -> None:
    p = T.pts.to_numpy()
    eq = np.cumsum(p)
    dd = (eq - np.maximum.accumulate(eq)).min()
    t = p.mean() / (p.std(ddof=1) / np.sqrt(len(p)))
    print(f"  {label:<18}{len(p):>6}{p.sum():>+10,.0f}{p.mean():>+9.2f}{t:>+7.1f}"
          f"{100*(p>0).mean():>7.0f}%{T.days.mean():>7.1f}{p.min():>+9.0f}"
          f"{dd:>+9.0f}{p.sum()/abs(dd):>+8.2f}")


def main() -> None:
    df = V.spx_daily()
    print(f"\n{'='*112}")
    print(f"IBS EXITS — same entry (IBS<0.20), {len(df):,} sessions, "
          f"net {COST}pt/RT")
    print(f"{'='*112}")
    print(f"  {'exit rule':<18}{'trades':>6}{'total':>10}{'per tr':>9}{'t':>7}"
          f"{'win%':>8}{'hold':>7}{'worst':>9}{'maxDD':>9}{'ret/DD':>8}")

    show(run(df, lambda i, j, s: s["ibs"][j] > 0.80), "thesis IBS>0.80")
    for x in (0.5, 0.6, 0.7, 0.9):
        show(run(df, lambda i, j, s, x=x: s["ibs"][j] > x), f"  IBS>{x:.1f}")
    print()
    for k in (0.5, 1.0, 2.0, 3.0):
        show(run(df, lambda i, j, s, k=k: s["c"][j] >= s["ent"] + k * s["atr"]),
             f"target {k}xATR")
    print()
    for k in (0.5, 1.0, 2.0):
        show(run(df, lambda i, j, s, k=k: s["c"][j] <= s["peak"] - k * s["atr"]),
             f"trail {k}xATR")
    print()
    show(run(df, lambda i, j, s: s["c"][j] > s["ent"]), "first up close")
    show(run(df, lambda i, j, s: s["c"][j] > s["h"][j - 1]), "close > prior hi")
    show(run(df, lambda i, j, s: (s["ibs"][j] > 0.80) or (j - i >= 10)),
         "thesis + 10d cap")
    print()
    for k in (3, 5, 10):
        show(run(df, lambda i, j, s, k=k: j - i >= k), f"hold {k}d (control)")
    print(f"\n  ret/DD = total divided by max equity drawdown.")
    print(f"{'='*112}\n")


if __name__ == "__main__":
    main()
