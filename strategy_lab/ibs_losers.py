"""Profile the IBS losers BEFORE hypothesising a single filter.

Previous attempts guessed a filter (200dMA, vol-scaling, gamma sizing, a
Donchian hedge), tested it, and reported a failure. That is testing hypotheses
in a vacuum. This does the thing that should have come first: take every trade,
measure everything knowable AT ENTRY, and look at what the bad ones have in
common -- especially the TAIL, since -321pt is the trade that sets position size
and four trades like it produce most of the drawdown.

Nothing here decides anything. It describes. The rule comes after.

    python ibs_losers.py
"""
from __future__ import annotations

import io
import time

import httpx
import numpy as np
import pandas as pd

import ibs_verify as V

COST = 0.517
IN_TH, OUT_TH = 0.20, 0.90


def vix() -> pd.Series:
    p1 = int(time.mktime(time.strptime("2009-06-01", "%Y-%m-%d")))
    u = (f"https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX"
         f"?period1={p1}&period2={int(time.time())}&interval=1d")
    r = httpx.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=60,
                  follow_redirects=True)
    res = r.json()["chart"]["result"][0]
    s = pd.Series(res["indicators"]["quote"][0]["close"],
                  index=pd.to_datetime(res["timestamp"], unit="s", utc=True))
    s = s.dropna()
    s.index = s.index.normalize()   # ^VIX and ^GSPC timestamps differ intraday
    return s


def trades_with_features(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l, o = df.c.to_numpy(), df.h.to_numpy(), df.l.to_numpy(), df.o.to_numpy()
    rngd = (df.h - df.l).replace(0, np.nan)
    ibs = ((df.c - df.l) / rngd).to_numpy()
    pc = df.c.shift(1)
    tr = pd.concat([df.h - df.l, (df.h - pc).abs(), (df.l - pc).abs()],
                   axis=1).max(axis=1)
    atr = tr.rolling(20).mean().to_numpy()
    ma20 = df.c.rolling(20).mean().to_numpy()
    ma50 = df.c.rolling(50).mean().to_numpy()
    ma200 = df.c.rolling(200).mean().to_numpy()
    hi20 = df.c.rolling(20).max().to_numpy()
    lo55 = df.c.rolling(55).min().to_numpy()
    ret = df.c.diff()
    up = (ret > 0).astype(int).to_numpy()
    dnstreak = np.zeros(len(df), dtype=int)
    for i in range(1, len(df)):
        dnstreak[i] = 0 if up[i] else dnstreak[i - 1] + 1
    # RSI(2)
    d = ret.to_numpy()
    g = pd.Series(np.where(d > 0, d, 0.0)).rolling(2).mean().to_numpy()
    ls = pd.Series(np.where(d < 0, -d, 0.0)).rolling(2).mean().to_numpy()
    rsi2 = 100 - 100 / (1 + g / np.where(ls == 0, 1e-9, ls))
    vx = vix().reindex(df.index.normalize()).ffill().to_numpy()
    vxma = pd.Series(vx).rolling(20).mean().to_numpy()

    n = len(df)
    rows, i = [], 0
    while i < n - 1:
        if not (np.isfinite(ibs[i]) and ibs[i] < IN_TH and np.isfinite(atr[i])
                and atr[i] > 0 and np.isfinite(ma200[i]) and np.isfinite(vx[i])):
            i += 1
            continue
        j = i + 1
        while j < n and not (np.isfinite(ibs[j]) and ibs[j] > OUT_TH):
            j += 1
        j = min(j, n - 1)
        rows.append({
            "in": df.index[i], "out": df.index[j], "days": j - i,
            "pts": c[j] - c[i] - COST,
            "ibs_val": ibs[i],
            "atr_pct": atr[i] / c[i] * 100,
            "vix": vx[i], "vix_rel": vx[i] / vxma[i],
            "d_ma20": (c[i] - ma20[i]) / atr[i],
            "d_ma50": (c[i] - ma50[i]) / atr[i],
            "d_ma200": (c[i] - ma200[i]) / atr[i],
            "dd_from_hi20": (c[i] - hi20[i]) / atr[i],
            "d_lo55": (c[i] - lo55[i]) / atr[i],
            "dnstreak": dnstreak[i],
            "rsi2": rsi2[i],
            "ret5": (c[i] - c[i - 5]) / atr[i],
            "ret20": (c[i] - c[i - 20]) / atr[i],
            "gap": (o[i] - c[i - 1]) / atr[i],
            "day_range": (h[i] - l[i]) / atr[i],
            "yr": df.index[i].year,
        })
        i = j + 1
    return pd.DataFrame(rows)


def main() -> None:
    df = V.spx_daily()
    T = trades_with_features(df)
    T["usd"] = T.pts
    L = T[T.pts < 0]
    TAIL = T.nsmallest(20, "pts")
    print(f"\n{'='*100}")
    print(f"IBS LOSER PROFILE — {len(T)} trades, {len(L)} losers "
          f"({100*len(L)/len(T):.0f}%), total {T.pts.sum():+,.0f}pt")
    print(f"  losers cost {L.pts.sum():+,.0f}pt; the WORST 20 alone cost "
          f"{TAIL.pts.sum():+,.0f}pt ({100*TAIL.pts.sum()/L.pts.sum():.0f}% of all losses)")
    print(f"{'='*100}")
    feats = ["ibs_val", "atr_pct", "vix", "vix_rel", "d_ma20", "d_ma50",
             "d_ma200", "dd_from_hi20", "d_lo55", "dnstreak", "rsi2", "ret5",
             "ret20", "gap", "day_range"]
    W = T[T.pts > 0]
    print(f"  {'feature':<14}{'winners':>10}{'losers':>10}{'WORST20':>10}"
          f"{'t(w20 vs win)':>15}")
    res = []
    for f in feats:
        w, tl = W[f].to_numpy(), TAIL[f].to_numpy()
        t = (tl.mean() - w.mean()) / np.sqrt(tl.var(ddof=1)/len(tl)
                                             + w.var(ddof=1)/len(w))
        res.append((abs(t), f, w.mean(), L[f].mean(), tl.mean(), t))
    for _, f, wm, lm, tm, t in sorted(res, reverse=True):
        print(f"  {f:<14}{wm:>10.2f}{lm:>10.2f}{tm:>10.2f}{t:>+15.1f}")
    print(f"\n  WORST 20 TRADES")
    print(f"  {'entry':<12}{'pts':>8}{'days':>6}{'vix':>7}{'vix_rel':>9}"
          f"{'d_ma200':>9}{'d_lo55':>8}{'dnstrk':>7}{'atr%':>7}")
    for r in TAIL.sort_values("pts").itertuples(index=False):
        print(f"  {r._0:%Y-%m-%d}  {r.pts:>+8.0f}{r.days:>6}{r.vix:>7.1f}"
              f"{r.vix_rel:>9.2f}{r.d_ma200:>9.1f}{r.d_lo55:>8.1f}"
              f"{r.dnstreak:>7}{r.atr_pct:>7.2f}")
    T.to_pickle(".cache_ibs_trades.pkl")
    print(f"{'='*100}\n")


if __name__ == "__main__":
    main()
