"""Breakout vs retest, at daily scale — 4,192 sessions instead of 72.

The 72-session ES tape cannot answer an intraday question: every cut of it lands
20-200 events, which is why nothing survives a cross-contract check. The same
structure -- break a level, then either run or come back to it -- exists on daily
bars, where there are 16 years of it.

Level = prior day's high (or low). A BREAK is a session whose high exceeds PDH.
Two entries, exactly the two being compared intraday:

    MARKET  : buy the close of the breaking session
    RETEST  : rest a limit at PDH; filled if a LATER session trades back to it
              within RETEST_WIN sessions. Misses count as ZERO, same
              opportunity set.

Then the question that actually matters: what, OBSERVABLE ON THE BREAK DAY,
separates the breaks that keep going from the ones that fail? With 4,192
sessions a feature scan can answer that; with 72 it cannot.

    python pdh_break.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import ibs_verify as V

RETEST_WIN = 5          # sessions the limit rests
HORIZONS = (1, 3, 5, 10)
COST_PT = 0.517


def build(df: pd.DataFrame, side: int = +1) -> pd.DataFrame:
    h, l, c, o = (df.h.to_numpy(), df.l.to_numpy(),
                  df.c.to_numpy(), df.o.to_numpy())
    n = len(df)
    tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]),
                                              abs(l[1:] - c[:-1])))
    atr = pd.Series(tr).rolling(20).mean().to_numpy()
    atr = np.concatenate([[np.nan], atr])
    ma50 = pd.Series(c).rolling(50).mean().to_numpy()
    ma200 = pd.Series(c).rolling(200).mean().to_numpy()
    rows = []
    for i in range(200, n - max(HORIZONS) - RETEST_WIN):
        lvl = h[i - 1] if side > 0 else l[i - 1]
        broke = (h[i] > lvl) if side > 0 else (l[i] < lvl)
        if not broke or not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        # must not have opened already beyond it -- a gap is not a break
        if (o[i] - lvl) * side > 0:
            continue
        rng = h[i] - l[i]
        if rng <= 0:
            continue
        # did a later session come back to the level?
        w = slice(i + 1, i + 1 + RETEST_WIN)
        back = (l[w] <= lvl) if side > 0 else (h[w] >= lvl)
        j = int(np.flatnonzero(back)[0]) + i + 1 if back.any() else None
        r = {"i": i, "date": df.index[i], "lvl": lvl, "side": side,
             "mkt": c[i], "filled": j is not None,
             # --- features, all known at the close of the break day ---
             "close_pos": ((c[i] - l[i]) / rng if side > 0
                           else (h[i] - c[i]) / rng),
             "body_frac": abs(c[i] - o[i]) / rng,
             "range_atr": rng / atr[i],
             "pen_atr": (c[i] - lvl) * side / atr[i],
             "gap_atr": (o[i] - c[i - 1]) * side / atr[i],
             "above_ma50": (c[i] - ma50[i]) * side / atr[i],
             "above_ma200": (c[i] - ma200[i]) * side / atr[i],
             "prior_ibs": (c[i - 1] - l[i - 1]) / max(h[i - 1] - l[i - 1], 1e-9),
             "run5_atr": (c[i] - c[i - 5]) * side / atr[i],
             "atr_pct": atr[i] / c[i] * 100.0,
             }
        for hz in HORIZONS:
            r[f"mkt_{hz}"] = (c[i + hz] - c[i]) * side
            r[f"ret_{hz}"] = ((c[j + hz] - lvl) * side
                              if j is not None and j + hz < n else np.nan)
        rows.append(r)
    return pd.DataFrame(rows)


def main() -> None:
    df = V.spx_daily()
    for side, nm in ((+1, "PDH break (long)"), (-1, "PDL break (short)")):
        E = build(df, side)
        print(f"\n{'='*96}")
        print(f"{nm} — {len(E):,} events over {len(df):,} sessions, "
              f"fill rate {100*E.filled.mean():.0f}%")
        print(f"{'='*96}")
        print(f"  {'horizon':<10}{'MARKET':>12}{'win%':>7}"
              f"{'RETEST/event':>14}{'RETEST|filled':>15}{'win%':>7}")
        for hz in HORIZONS:
            mk = E[f"mkt_{hz}"] - COST_PT
            rt = E[f"ret_{hz}"] - COST_PT
            print(f"  {str(hz)+'d':<10}{mk.mean():>+12.2f}"
                  f"{100*(mk>0).mean():>6.0f}%{rt.fillna(0).mean():>+14.2f}"
                  f"{rt.dropna().mean():>+15.2f}"
                  f"{100*(rt.dropna()>0).mean():>6.0f}%")

        feats = ["close_pos", "body_frac", "range_atr", "pen_atr", "gap_atr",
                 "above_ma50", "above_ma200", "prior_ibs", "run5_atr", "atr_pct"]
        print(f"\n  FEATURE SCAN — 5-day forward from the market entry, by tercile")
        print(f"  {'feature':<14}{'bottom 1/3':>12}{'mid':>10}{'top 1/3':>10}"
              f"{'spread':>10}{'t(top-bot)':>12}")
        res = []
        for f in feats:
            x = E[f].to_numpy(dtype=float)
            y = (E["mkt_5"] - COST_PT).to_numpy()
            ok = np.isfinite(x) & np.isfinite(y)
            q1, q2 = np.nanpercentile(x[ok], [33, 67])
            b, m_, t_ = y[ok & (x <= q1)], y[ok & (x > q1) & (x < q2)], y[ok & (x >= q2)]
            tt = (t_.mean() - b.mean()) / np.sqrt(t_.var(ddof=1)/len(t_)
                                                  + b.var(ddof=1)/len(b))
            res.append((abs(tt), f, b.mean(), m_.mean(), t_.mean(),
                        t_.mean() - b.mean(), tt))
        for _, f, b, m_, t_, sp, tt in sorted(res, reverse=True):
            print(f"  {f:<14}{b:>+12.2f}{m_:>+10.2f}{t_:>+10.2f}"
                  f"{sp:>+10.2f}{tt:>+12.1f}")
        E.to_pickle(f".cache_pdh_{'up' if side > 0 else 'dn'}.pkl")
    print(f"\n{'='*96}\n")


if __name__ == "__main__":
    main()
