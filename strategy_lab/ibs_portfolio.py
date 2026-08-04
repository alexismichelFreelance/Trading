"""IBS across 8 indices: is the diversification real, or one trade in 8 costumes?

IBS is positive on 8 of 8 world indices (ibs_verify / the cross-market run), which
raises the only question that matters for sizing a portfolio of them: do they
fire together, lose together, and is anything leading anything else?

Four questions, answered separately because they have different answers:

  1. TIMING     -- when one index signals, how many others signal too?
  2. LEAD/LAG   -- Tokyo closes ~14h before New York on the SAME calendar date,
                   so if Asia's IBS predicts the US's, that is a usable
                   same-day lead, not a lagged curiosity.
  3. LOSSES     -- correlation of winners is fine; correlation of LOSSES is what
                   determines whether the portfolio has a bad month or a
                   catastrophe. Measured separately from overall correlation.
  4. PORTFOLIO  -- equal-weight 8 vs the average single index. Diversification is
                   only real if drawdown falls FASTER than return.

All trades are long-only by construction, so "same direction" is not in
question -- the exposure question is how much of the time they overlap.

    python ibs_portfolio.py
"""
from __future__ import annotations

import httpx
import numpy as np
import pandas as pd

COST_F = 0.0004
IN_TH, OUT_TH = 0.20, 0.90
IDX = {"^GSPC": "S&P 500", "^NDX": "Nasdaq 100", "^RUT": "Russell 2000",
       "^DJI": "Dow 30", "^GDAXI": "DAX", "^FTSE": "FTSE 100",
       "^N225": "Nikkei", "^STOXX50E": "EuroStoxx 50"}
# rough close order within a calendar date -- the lead/lag axis
ORDER = ["^N225", "^FTSE", "^GDAXI", "^STOXX50E", "^GSPC", "^NDX", "^DJI", "^RUT"]


def daily(sym: str) -> pd.DataFrame:
    u = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
         f"{sym.replace('^', '%5E')}?range=2y&interval=1h")
    j = httpx.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=60,
                  follow_redirects=True).json()["chart"]["result"][0]
    q = j["indicators"]["quote"][0]
    H = pd.DataFrame({"h": q["high"], "l": q["low"], "c": q["close"]},
                     index=pd.to_datetime(j["timestamp"], unit="s", utc=True)).dropna()
    H["d"] = H.index.normalize()
    rows = []
    for d, g in H.groupby("d"):
        if len(g) < 5:
            continue
        hi, lo, c = g.h.max(), g.l.min(), g.c.iloc[-1]
        if hi <= lo:
            continue
        rows.append({"d": d, "c": c, "ibs": (c - lo) / (hi - lo)})
    return pd.DataFrame(rows).set_index("d")


def sleeve(D: pd.DataFrame) -> pd.DataFrame:
    """Daily position (0/1, set at the close) and next-day P&L in % of price."""
    c, ib = D.c.to_numpy(), D.ibs.to_numpy()
    n = len(D)
    pos = np.zeros(n)
    hold = False
    for i in range(n):
        if not hold and ib[i] < IN_TH:
            hold = True
        elif hold and ib[i] > OUT_TH:
            hold = False
        pos[i] = 1.0 if hold else 0.0
    ret = np.zeros(n)
    ret[1:] = pos[:-1] * (c[1:] / c[:-1] - 1.0)
    turn = np.abs(np.diff(pos, prepend=0.0))
    return pd.DataFrame({"pos": pos, "sig": (ib < IN_TH).astype(float),
                         "pnl": 100 * (ret - turn * COST_F)}, index=D.index)


def main() -> None:
    S = {}
    for sym in IDX:
        try:
            S[sym] = sleeve(daily(sym))
        except Exception as ex:
            print(f"  {sym}: {type(ex).__name__}")
    P = pd.DataFrame({s: S[s].pnl for s in S}).fillna(0.0)
    G = pd.DataFrame({s: S[s].pos for s in S}).reindex(P.index).ffill().fillna(0.0)
    SG = pd.DataFrame({s: S[s].sig for s in S}).reindex(P.index).fillna(0.0)

    print(f"\n{'='*98}")
    print(f"IBS ACROSS {len(S)} INDICES — {len(P)} calendar days, 2y hourly-derived")
    print(f"{'='*98}")

    print("\n1. TIMING — how many indices are IN A POSITION on the same day?")
    k = G.sum(axis=1)
    act = k[k > 0]
    for lo, hi in ((1, 2), (2, 4), (4, 6), (6, 9)):
        n = int(((k >= lo) & (k < hi)).sum())
        print(f"   {lo}-{hi-1} indices long: {n:>4} days ({100*n/len(P):>4.0f}% of all days)")
    print(f"   mean simultaneous longs when any is on: {act.mean():.1f} of {len(S)}")
    print(f"   ALL {len(S)} long at once: {int((k == len(S)).sum())} days")

    print("\n2. LEAD / LAG — corr of ENTRY SIGNALS at lag k (k>0 = row leads col)")
    print("   strongest cross-pair at each lag, and Asia->US specifically")
    for lag in (-2, -1, 0, 1, 2):
        best, bv = None, 0.0
        for a in SG.columns:
            for b in SG.columns:
                if a == b:
                    continue
                v = SG[a].corr(SG[b].shift(-lag))
                if abs(v) > abs(bv):
                    best, bv = (a, b), v
        n_us = SG["^N225"].corr(SG["^GSPC"].shift(-lag))
        print(f"   lag {lag:+d}d: best {IDX[best[0]]:>13} -> {IDX[best[1]]:<13} "
              f"{bv:+.2f}   |  Nikkei -> S&P {n_us:+.2f}")

    print("\n3. LOSSES — do the bad days coincide?")
    allc = P.corr().values[np.triu_indices(len(S), 1)]
    dn = P[P.mean(axis=1) < 0]
    dnc = dn.corr().values[np.triu_indices(len(S), 1)]
    print(f"   mean pairwise corr, ALL days      {allc.mean():+.2f}")
    print(f"   mean pairwise corr, DOWN days     {dnc.mean():+.2f}   <- the one that matters")
    tot = P.sum(axis=1)
    w5 = tot.nsmallest(5)
    print(f"   on the 5 worst portfolio days, how many of {len(S)} were losing:")
    for d, v in w5.items():
        nl = int((P.loc[d] < 0).sum())
        print(f"     {d:%Y-%m-%d}  {v:+6.2f}%   {nl}/{len(S)} losing")

    print("\n4. PORTFOLIO — equal weight vs the average single index")
    def stat(x):
        eq = np.cumsum(x)
        dd = (eq - np.maximum.accumulate(eq)).min()
        return x.sum(), dd, x.sum() / abs(dd) if dd < 0 else np.inf
    singles = [stat(P[s].to_numpy()) for s in P.columns]
    port = stat(P.mean(axis=1).to_numpy())
    print(f"   {'':<22}{'total%':>9}{'maxDD%':>9}{'ret/DD':>9}")
    print(f"   {'average single index':<22}{np.mean([s[0] for s in singles]):>+9.1f}"
          f"{np.mean([s[1] for s in singles]):>+9.2f}"
          f"{np.mean([s[2] for s in singles]):>+9.2f}")
    print(f"   {'equal-weight 8':<22}{port[0]:>+9.1f}{port[1]:>+9.2f}{port[2]:>+9.2f}")
    print(f"   {'best single':<22}{max(s[0] for s in singles):>+9.1f}"
          f"{'':>9}{max(s[2] for s in singles):>+9.2f}")
    print(f"{'='*98}\n")


if __name__ == "__main__":
    main()
