"""The exhaustion score on 88 sessions of purchased CME data, not 10 of ours.

Same construction as exhaustion_score.py, same pre-committed reading rules, run
on ESH5/ESM5 minute bars built from 1.13bn MBO rows (strategy_lab/mbo_minutes.py).
Feb-May 2025: an entirely different regime, different contracts, and no overlap
whatsoever with the 2026 live record the idea came from. If the gradient is a
property of markets it should survive that. If it was a property of ten August
sessions it should not.

READING RULES, fixed before running and unchanged from the live version:
  * the question is CONDITIONAL -- does the forward distribution shift
  * the control is EXTENDED-vs-EXTENDED, never extended-vs-quiet
  * full distributions, not a mean and a verdict
  * a monotone gradient matters more than any single decile
  * firing rarely is a feature

WHAT THE LIVE VERSION FOUND (10 sessions, ES+NQ 2026): the median forward return
did NOT move across score deciles, but the left tail did -- p25 fell from +0.022
to -0.124 of a daily range at 60 minutes, down% rose 19%->34%, and the mean
flipped positive to negative. Both instruments agreed in direction. That is a
RISK signal, not a direction signal, and it is what makes scaling out rational
rather than flattening.

The specific thing to check here is whether that same shape -- flat median,
deteriorating tail -- appears in 2025.
"""
import sys

import numpy as np
import pandas as pd

FWD = (15, 30, 60)
SRC = r"D:\Trading\strategy_lab\mbo_minutes.csv"


def build():
    m = pd.read_csv(SRC)
    m = m[(m["mod"] >= 570) & (m["mod"] < 960)].copy()
    out, prior = [], []
    for day, d in m.groupby("day", sort=True):
        d = d.sort_values("mod").reset_index(drop=True)
        if len(d) < 200:
            continue
        med = float(np.median(prior[-10:])) if len(prior) >= 3 else np.nan
        d["delta"] = d.buy_v - d.sell_v
        d["buy_sz"] = d.buy_v / d.buy_n.replace(0, np.nan)
        d["sell_sz"] = d.sell_v / d.sell_n.replace(0, np.nan)
        d["hh"] = d.px.cummax()
        d["ll"] = d.px.cummin()
        d["rng"] = d.hh - d.ll
        d["range_used"] = d.rng / med if med and med > 0 else np.nan
        d["from_hi"] = (d.hh - d.px) / d.rng.replace(0, np.nan)
        d["conv"] = d.delta.abs() / d.vol.replace(0, np.nan)
        d["dom"] = d.buy_sz / d.sell_sz.replace(0, np.nan)
        d["conv_ref"] = d.conv.shift(1).rolling(15, min_periods=8).median()
        d["dom_ref"] = d.dom.shift(1).rolling(15, min_periods=8).median()
        d["vol_ref"] = d.vol.shift(1).expanding(20).median()
        for f in FWD:
            d["fwd%d" % f] = (d.px.shift(-f) - d.px) / med if med and med > 0 else np.nan
        out.append(d)
        prior.append(float(d.px.max() - d.px.min()))
    return pd.concat(out, ignore_index=True)


def main():
    A = build().dropna(subset=["range_used", "conv", "dom", "conv_ref",
                               "dom_ref", "vol_ref"])
    print("%d minute rows, %d sessions, %s -> %s"
          % (len(A), A.day.nunique(), A.day.min(), A.day.max()))
    near = A[(A.from_hi <= 0.15) & (A.range_used >= 0.6)].copy()
    print("eligible (near session high, day >=0.6 of a normal range): "
          "%d rows over %d sessions\n" % (len(near), near.day.nunique()))

    near["c_drop"] = 1.0 - near.conv / near.conv_ref.replace(0, np.nan)
    near["d_drop"] = near.dom_ref - near.dom
    near["part"] = near.vol / near.vol_ref.replace(0, np.nan)
    for c in ("c_drop", "d_drop", "part"):
        near[c + "_r"] = near[c].rank(pct=True)
    near["score"] = near[["c_drop_r", "d_drop_r", "part_r"]].mean(axis=1)

    for f in FWD:
        col = "fwd%d" % f
        d = near.dropna(subset=[col, "score"]).copy()
        if len(d) < 300:
            continue
        d["dec"] = pd.qcut(d.score, 10, labels=False, duplicates="drop")
        print("  forward %d min, by exhaustion-score decile "
              "(units = median daily range)" % f)
        print("    %-4s %6s %8s %8s %8s %7s" %
              ("dec", "n", "median", "mean", "p25", "down%"))
        for k, g in d.groupby("dec"):
            print("    %-4d %6d %+8.3f %+8.3f %+8.3f %6.0f%%"
                  % (k, len(g), g[col].median(), g[col].mean(),
                     g[col].quantile(.25), 100 * (g[col] < 0).mean()))
        top, bot = d[d.dec >= 8], d[d.dec <= 1]
        print("    top2 vs bot2:  median %+.3f vs %+.3f   p25 %+.3f vs %+.3f   "
              "down %.0f%% vs %.0f%%\n"
              % (top[col].median(), bot[col].median(),
                 top[col].quantile(.25), bot[col].quantile(.25),
                 100 * (top[col] < 0).mean(), 100 * (bot[col] < 0).mean()))


if __name__ == "__main__":
    main()
