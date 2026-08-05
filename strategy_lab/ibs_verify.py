"""IBS daily mean-reversion — independent re-derivation, net of costs.

QUIET_CLASSICS_SCREEN.md reports IBS as the project's strongest survivor
(16y, t=3.7, no decay) but the script behind it was not kept, so the number has
never been reproduced. This re-derives it from scratch and, more importantly,
runs the year SINCE that screen was written (it ended 2025-07) as untouched
out-of-sample data.

Rule (unchanged from the screen, no parameters fitted here):
    IBS = (C - L) / (H - L)
    flat and IBS < 0.20  -> buy at the close
    long and IBS > 0.80  -> sell at the close

Reported net of 0.517 pt round-trip (es_costs, aggressive preset) so the number
is comparable to everything else in the lab. Exposure is reported because a
mean/day figure on a sleeve that is only in the market a third of the time is
not comparable to buy-and-hold without it.

    python ibs_verify.py
"""
from __future__ import annotations

import time

import httpx
import numpy as np
import pandas as pd

COST_PT = 0.517            # ES round-turn, aggressive preset (es_costs.py)
IN_TH, OUT_TH = 0.20, 0.80
SPLIT = "2018-01-01"       # H1 / H2 decay guard, as in the screen
OOS = "2025-07-01"         # everything after the screen was written


def spx_daily() -> pd.DataFrame:
    p1 = int(time.mktime(time.strptime("2009-12-01", "%Y-%m-%d")))
    u = (f"https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
         f"?period1={p1}&period2={int(time.time())}&interval=1d")
    r = httpx.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=60,
                  follow_redirects=True)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({"o": q["open"], "h": q["high"], "l": q["low"],
                       "c": q["close"], "v": q["volume"]},
                      index=pd.to_datetime(res["timestamp"], unit="s", utc=True))
    return df.dropna()


def trades(df: pd.DataFrame) -> pd.DataFrame:
    """Long-only, one unit, entry and exit both at the close. No stop -- the
    classic spec; stops break daily mean reversion (see ibs_swing.py)."""
    rng = (df.h - df.l).replace(0, np.nan)
    ibs = ((df.c - df.l) / rng).to_numpy()
    c = df.c.to_numpy()
    idx = df.index
    out, pos, ent, ent_i = [], 0, 0.0, 0
    for i in range(len(df)):
        if np.isnan(ibs[i]):
            continue
        if pos == 0 and ibs[i] < IN_TH:
            pos, ent, ent_i = 1, c[i], i
        elif pos == 1 and ibs[i] > OUT_TH:
            out.append({"in": idx[ent_i], "out": idx[i], "days": i - ent_i,
                        "pts": c[i] - ent - COST_PT})
            pos = 0
    return pd.DataFrame(out)


def stats(T: pd.DataFrame, n_days: int, label: str) -> None:
    if T.empty:
        print(f"  {label:<22} no trades")
        return
    p = T.pts.to_numpy()
    t = p.mean() / (p.std(ddof=1) / np.sqrt(len(p)))
    in_mkt = T.days.sum()
    print(f"  {label:<22}{len(p):>6}{p.sum():>+11,.0f}{p.mean():>+9.2f}"
          f"{t:>+7.1f}{100*(p>0).mean():>7.0f}%{T.days.mean():>8.1f}"
          f"{100*in_mkt/n_days:>8.0f}%{p.sum()/max(in_mkt,1):>+9.2f}")


def main() -> None:
    df = spx_daily()
    print(f"\n{'='*104}")
    print(f"IBS DAILY MEAN-REVERSION — SPX, {len(df):,} sessions "
          f"{df.index[0]:%Y-%m-%d} to {df.index[-1]:%Y-%m-%d}, NET of "
          f"{COST_PT:.3f}pt round-turn")
    print(f"{'='*104}")
    print(f"  {'period':<22}{'trades':>6}{'total pt':>11}{'pt/trade':>9}"
          f"{'t':>7}{'win%':>8}{'hold d':>8}{'expos':>8}{'pt/day-in':>9}")

    full = trades(df)
    stats(full, len(df), "FULL 2010-now")
    h1 = df[df.index < SPLIT]
    h2 = df[(df.index >= SPLIT) & (df.index < OOS)]
    oos = df[df.index >= OOS]
    stats(trades(h1), len(h1), f"H1 ->{SPLIT[:4]}")
    stats(trades(h2), len(h2), f"H2 {SPLIT[:4]}-{OOS[:4]}")
    stats(trades(oos), len(oos), f"OUT-OF-SAMPLE {OOS[:7]}+")

    # buy-and-hold baseline over the same span, per calendar day
    bh = df.c.iloc[-1] - df.c.iloc[0]
    print(f"\n  buy & hold over the same span: {bh:+,.0f} pt "
          f"({bh/len(df):+.2f} pt/day, 100% exposure)")
    p = full.pts.to_numpy()
    print(f"  IBS in-market efficiency:      {p.sum()/full.days.sum():+.2f} pt "
          f"per day IN the market, at {100*full.days.sum()/len(df):.0f}% exposure")
    eq = np.cumsum(p)
    print(f"  worst trade {p.min():+,.0f} pt   max equity drawdown "
          f"{(eq - np.maximum.accumulate(eq)).min():+,.0f} pt")
    print(f"{'='*104}\n")


if __name__ == "__main__":
    main()
