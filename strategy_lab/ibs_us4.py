"""IBS on the four US indices — the portfolio that is actually executable.

The 8-index result (ibs_portfolio.py) is real but not reachable: it needs margin
and data on three continents. ES / NQ / RTY / YM sit in one account, on one feed,
with one clearing relationship, and micros exist for all four -- which matters,
because a single full-size ES contract needs ~$190k at 15% drawdown tolerance
and the micros are 1/10 of that.

This runs the same test on 16 YEARS of daily data instead of 2 years of hourly,
so the diversification number rests on ~3,900 sessions and several regimes
rather than one crash.

Trades are index-level; the futures track them closely but not exactly (futures
close later than the cash index), so treat these as the strategy's numbers, not
a fill simulation.

    python ibs_us4.py
"""
from __future__ import annotations

import time

import httpx
import numpy as np
import pandas as pd

COST_F = 0.0004                     # ~4bp round turn, index-agnostic
IN_TH, OUT_TH = 0.20, 0.90
US4 = {"^GSPC": "S&P 500 (ES)", "^NDX": "Nasdaq 100 (NQ)",
       "^RUT": "Russell 2000 (RTY)", "^DJI": "Dow 30 (YM)"}
SPLIT, OOS = "2018-01-01", "2025-07-01"


def daily(sym: str) -> pd.DataFrame:
    p1 = int(time.mktime(time.strptime("2009-12-01", "%Y-%m-%d")))
    u = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
         f"{sym.replace('^', '%5E')}?period1={p1}&period2={int(time.time())}"
         f"&interval=1d")
    j = httpx.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=60,
                  follow_redirects=True).json()["chart"]["result"][0]
    q = j["indicators"]["quote"][0]
    df = pd.DataFrame({"h": q["high"], "l": q["low"], "c": q["close"]},
                      index=pd.to_datetime(j["timestamp"], unit="s", utc=True)
                      ).dropna()
    df.index = df.index.normalize()
    return df


def sleeve(D: pd.DataFrame) -> pd.DataFrame:
    rng = (D.h - D.l).replace(0, np.nan)
    ibs = ((D.c - D.l) / rng).to_numpy()
    c = D.c.to_numpy()
    n = len(D)
    pos = np.zeros(n)
    hold = False
    for i in range(n):
        if np.isnan(ibs[i]):
            pos[i] = 1.0 if hold else 0.0
            continue
        if not hold and ibs[i] < IN_TH:
            hold = True
        elif hold and ibs[i] > OUT_TH:
            hold = False
        pos[i] = 1.0 if hold else 0.0
    r = np.zeros(n)
    r[1:] = pos[:-1] * (c[1:] / c[:-1] - 1.0)
    turn = np.abs(np.diff(pos, prepend=0.0))
    return pd.DataFrame({"pos": pos, "pnl": 100 * (r - turn * COST_F)},
                        index=D.index)


def stat(x: np.ndarray) -> tuple[float, float, float, float]:
    eq = np.cumsum(x)
    dd = (eq - np.maximum.accumulate(eq)).min()
    nz = x[x != 0]
    t = nz.mean() / (nz.std(ddof=1) / np.sqrt(len(nz))) if len(nz) > 2 else 0.0
    return x.sum(), dd, (x.sum() / abs(dd) if dd < 0 else np.inf), t


def main() -> None:
    S = {s: sleeve(daily(s)) for s in US4}
    P = pd.DataFrame({s: S[s].pnl for s in S}).dropna(how="all").fillna(0.0)
    G = pd.DataFrame({s: S[s].pos for s in S}).reindex(P.index).ffill().fillna(0.0)

    print(f"\n{'='*96}")
    print(f"IBS — FOUR US INDICES, {len(P):,} sessions "
          f"{P.index[0]:%Y-%m} to {P.index[-1]:%Y-%m}, net {COST_F*1e4:.0f}bp/RT")
    print(f"{'='*96}")
    print(f"  {'index':<20}{'total%':>9}{'maxDD%':>9}{'ret/DD':>9}{'t':>7}"
          f"{'exposure':>10}")
    for s, nm in US4.items():
        t_, dd, rr, tt = stat(P[s].to_numpy())
        print(f"  {nm:<20}{t_:>+9.1f}{dd:>+9.2f}{rr:>+9.2f}{tt:>+7.1f}"
              f"{100*G[s].mean():>9.0f}%")

    print(f"\n  PAIRWISE DAILY P&L CORRELATION")
    C = P.corr()
    print("  " + " " * 20 + "".join(f"{US4[s].split()[0]:>10}" for s in US4))
    for a in US4:
        print(f"  {US4[a]:<20}" + "".join(f"{C.loc[a, b]:>10.2f}" for b in US4))
    off = C.values[np.triu_indices(len(US4), 1)]
    dn = P[P.mean(axis=1) < 0]
    offdn = dn.corr().values[np.triu_indices(len(US4), 1)]
    print(f"\n  mean off-diagonal: all days {off.mean():+.2f}   "
          f"down days {offdn.mean():+.2f}")

    k = G.sum(axis=1)
    print(f"  all four long simultaneously: {int((k == 4).sum())} of "
          f"{int((k > 0).sum())} active days "
          f"({100*(k == 4).sum()/max((k > 0).sum(), 1):.0f}%)")

    print(f"\n  PORTFOLIO")
    print(f"  {'':<20}{'total%':>9}{'maxDD%':>9}{'ret/DD':>9}")
    singles = [stat(P[s].to_numpy()) for s in P.columns]
    port = stat(P.mean(axis=1).to_numpy())
    spx = stat(P["^GSPC"].to_numpy())
    print(f"  {'average single':<20}{np.mean([s[0] for s in singles]):>+9.1f}"
          f"{np.mean([s[1] for s in singles]):>+9.2f}"
          f"{np.mean([s[2] for s in singles]):>+9.2f}")
    print(f"  {'S&P alone':<20}{spx[0]:>+9.1f}{spx[1]:>+9.2f}{spx[2]:>+9.2f}")
    print(f"  {'equal-weight 4':<20}{port[0]:>+9.1f}{port[1]:>+9.2f}{port[2]:>+9.2f}")

    print(f"\n  SUB-PERIODS (ret/DD)")
    print(f"  {'':<20}{'H1 ->2018':>12}{'H2 2018-25':>12}{'OOS 25/07+':>12}")
    for lab, ser in (("S&P alone", P["^GSPC"]), ("equal-weight 4", P.mean(axis=1))):
        row = f"  {lab:<20}"
        for lo, hi in ((None, SPLIT), (SPLIT, OOS), (OOS, None)):
            m = np.ones(len(P), bool)
            if lo:
                m &= np.asarray(P.index >= lo)
            if hi:
                m &= np.asarray(P.index < hi)
            row += f"{stat(ser.to_numpy()[m])[2]:>12.2f}"
        print(row)
    print(f"{'='*96}\n")


if __name__ == "__main__":
    main()
