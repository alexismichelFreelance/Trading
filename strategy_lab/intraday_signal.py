"""Is there ANY intraday signal in the recorded order flow? Settled at n=1.7M.

Every intraday test so far has been run at the "setup" level -- 236 breakouts,
60 IBS days, 8,515 sweeps -- and nothing survived a cross-contract check. That
is not evidence of no signal; it is evidence that the event definitions threw
away 99.99% of the data. The same 72 sessions contain 1.7M seconds of recorded
aggressor and book flow.

This asks the question at the level the data supports: given everything
observable at second t, is the price at t+H predictable at all?

  * every feature is causal -- rolling windows ending at t, nothing forward.
  * FIT/LOOK on ESH5 (Feb-Mar 2025), TEST on ESM5 (Mar-May 2025). Disjoint
    periods, different contracts. Nothing is chosen on the test set.
  * scored by information coefficient (rank corr with the forward move) and by
    the top-minus-bottom decile spread in TICKS, which is what a sleeve would
    actually capture.

pxc is the second's VWAP, not a traded price (see replay_questdb.py) -- that
biases FILLS, which is why every number here is a forecast statistic and not a
P&L. A signal found here still has to survive execution; no signal here means
there is nothing to execute on.

    python intraday_signal.py
"""
from __future__ import annotations

import io
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

URL = "http://localhost:9000"
TICK = 0.25
CACHE = Path(".cache_intraday")
RTH = ("13:30:00", "20:00:00")          # UTC, = 09:30-16:00 ET
HORIZONS = (30, 60, 300)
TABLES = {"ESH5": "claude_sec_feat_esh5", "ESM5": "claude_sec_feat"}


def load(sym: str) -> pd.DataFrame:
    CACHE.mkdir(exist_ok=True)
    f = CACHE / f"{sym}.pkl"
    if f.exists():
        return pd.read_pickle(f)
    sql = (f"SELECT ts, day, pxc, adelta, avol, ntr, bid_cancel, ask_cancel, "
           f"bid_add, ask_add FROM {TABLES[sym]} ORDER BY ts")
    r = httpx.get(f"{URL}/exp", params={"query": sql}, timeout=1200.0)
    r.raise_for_status()
    d = pd.read_csv(io.StringIO(r.text))
    d["ts"] = pd.to_datetime(d.ts, utc=True)
    d = d[(d.ts.dt.strftime("%H:%M:%S") >= RTH[0])
          & (d.ts.dt.strftime("%H:%M:%S") < RTH[1])]
    d.to_pickle(f)
    return d


def featurise(d: pd.DataFrame) -> pd.DataFrame:
    """All windows END at t. Grouped by session so nothing spans the overnight."""
    out = []
    for _, g in d.groupby("day", sort=True):
        g = g.sort_values("ts").copy()
        g["px"] = g.pxc.ffill()
        ad, px = g.adelta.fillna(0.0), g.px
        book = ((g.bid_add - g.bid_cancel) - (g.ask_add - g.ask_cancel)).fillna(0.0)
        for w in (5, 15, 60, 300):
            g[f"ofi{w}"] = ad.rolling(w, min_periods=w).sum()
            g[f"book{w}"] = book.rolling(w, min_periods=w).sum()
            g[f"mom{w}"] = (px - px.shift(w)) / TICK
        g["vol60"] = px.diff().rolling(60, min_periods=60).std() / TICK
        g["intens"] = g.ntr.rolling(60, min_periods=60).sum()
        # flow normalised by its own recent scale -- a raw count is not
        # comparable between a quiet 11:00 and the close
        g["ofi60z"] = g.ofi60 / ad.rolling(600, min_periods=300).std().replace(0, np.nan)
        g["book60z"] = g.book60 / book.rolling(600, min_periods=300).std().replace(0, np.nan)
        g["tod"] = g.ts.dt.hour * 60 + g.ts.dt.minute
        for h in HORIZONS:
            g[f"y{h}"] = (px.shift(-h) - px) / TICK
        out.append(g)
    return pd.concat(out, ignore_index=True)


FEATS = ["ofi5", "ofi15", "ofi60", "ofi300", "ofi60z",
         "book5", "book15", "book60", "book300", "book60z",
         "mom5", "mom15", "mom60", "mom300", "vol60", "intens"]


def score(F: pd.DataFrame, feat: str, h: int) -> tuple[float, float, float, int]:
    x, y = F[feat].to_numpy(float), F[f"y{h}"].to_numpy(float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if len(x) < 5000:
        return np.nan, np.nan, np.nan, len(x)
    ic = pd.Series(x).corr(pd.Series(y), method="spearman")
    q = np.quantile(x, [0.1, 0.9])
    top, bot = y[x >= q[1]], y[x <= q[0]]
    spread = top.mean() - bot.mean()
    t = spread / np.sqrt(top.var(ddof=1)/len(top) + bot.var(ddof=1)/len(bot))
    return ic, spread, t, len(x)


def main() -> None:
    F = {s: featurise(load(s)) for s in TABLES}
    for s, d in F.items():
        print(f"  {s}: {len(d):,} RTH seconds, {d.day.nunique()} sessions")

    for h in HORIZONS:
        print(f"\n{'='*94}")
        print(f"FORWARD {h}s — decile spread in TICKS  "
              f"(fit-side ESH5, out-of-sample ESM5)")
        print(f"{'='*94}")
        print(f"  {'feature':<10}{'ESH5 IC':>9}{'spread':>9}{'t':>7}   "
              f"{'ESM5 IC':>9}{'spread':>9}{'t':>7}   verdict")
        rows = []
        for f in FEATS:
            a = score(F["ESH5"], f, h)
            b = score(F["ESM5"], f, h)
            rows.append((abs(a[0]) if np.isfinite(a[0]) else 0, f, a, b))
        for _, f, a, b in sorted(rows, reverse=True):
            same = (np.isfinite(a[2]) and np.isfinite(b[2])
                    and np.sign(a[1]) == np.sign(b[1])
                    and abs(a[2]) > 3 and abs(b[2]) > 3)
            v = "REPLICATES" if same else ""
            print(f"  {f:<10}{a[0]:>+9.4f}{a[1]:>+9.2f}{a[2]:>+7.1f}   "
                  f"{b[0]:>+9.4f}{b[1]:>+9.2f}{b[2]:>+7.1f}   {v}")
    print(f"\n  IC is Spearman rank correlation with the forward move. spread is")
    print(f"  top-decile minus bottom-decile mean forward move, in ticks — what a")
    print(f"  sleeve trading the extremes would capture before costs (~1 tick).")
    print(f"{'='*94}\n")


if __name__ == "__main__":
    main()
