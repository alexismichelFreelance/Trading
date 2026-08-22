"""The same question, without the thresholds that made the last test unmeasurable.

exhaustion_generalise.py required four conditions at once and fired on 31 of
2,410 eligible minutes. Every cutoff in it was invented by me, and each one
discarded data until nothing was left to measure. n=31 cannot separate "no edge"
from "cannot see".

This scores EVERY eligible minute continuously and compares by decile, so the
whole sample is used and no threshold is chosen:

  conviction drop   1 - conv / conv_ref     how far net conviction has fallen
                                            below its own recent level
  dominance flip    dom_ref - dom           how far buy-size dominance has
                                            fallen versus its own recent level
  participation     vol / vol_ref           still trading, i.e. not just quiet

Each is converted to a within-sample RANK (0..1) so the three combine without
units or weights, then averaged. High score = conviction gone, size dominance
gone, volume still there. That is the 2026-08-21 shape, expressed as a degree
rather than a yes/no.

READ IT AS A GRADIENT. The question is whether the forward distribution shifts
MONOTONICALLY across deciles -- a real effect should show a trend, not a single
lucky bucket. One outlying decile in an otherwise flat set is noise, and a clean
gradient is meaningful even if the extremes are only 55/45.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd

sys.path.insert(0, r"D:\Trading\strategy_lab")
from exhaustion_generalise import build, FWD


def main():
    A = pd.concat([build(s) for s in ("ES", "NQ")], ignore_index=True)
    A = A.dropna(subset=["range_used", "conv", "dom", "conv_ref", "dom_ref",
                         "vol_ref"])
    near = A[(A.from_hi <= 0.15) & (A.range_used >= 0.6)].copy()
    print("%d eligible minutes (near session high, day >=0.6 of a normal range), "
          "%d sessions" % (len(near), near.day.nunique()))

    near["c_drop"] = 1.0 - near.conv / near.conv_ref.replace(0, np.nan)
    near["d_drop"] = near.dom_ref - near.dom
    near["part"] = near.vol / near.vol_ref.replace(0, np.nan)
    for c in ("c_drop", "d_drop", "part"):
        near[c + "_r"] = near[c].rank(pct=True)
    near["score"] = near[["c_drop_r", "d_drop_r", "part_r"]].mean(axis=1)

    for f in FWD:
        col = "fwd%d" % f
        d = near.dropna(subset=[col, "score"]).copy()
        if len(d) < 200:
            continue
        d["dec"] = pd.qcut(d.score, 10, labels=False, duplicates="drop")
        print("\n  forward %d min, by exhaustion-score decile "
              "(units = median daily range)" % f)
        print("    %-4s %6s %8s %8s %8s %7s" %
              ("dec", "n", "median", "mean", "p25", "down%"))
        for k, g in d.groupby("dec"):
            print("    %-4d %6d %+8.3f %+8.3f %+8.3f %6.0f%%"
                  % (k, len(g), g[col].median(), g[col].mean(),
                     g[col].quantile(.25), 100 * (g[col] < 0).mean()))
        top, bot = d[d.dec >= 8], d[d.dec <= 1]
        print("    top2 vs bottom2:  median %+.3f vs %+.3f    down %.0f%% vs %.0f%%"
              % (top[col].median(), bot[col].median(),
                 100 * (top[col] < 0).mean(), 100 * (bot[col] < 0).mean()))
        for s in ("ES", "NQ"):
            ds = d[d.sym == s]
            if len(ds) < 100:
                continue
            t2, b2 = ds[ds.dec >= 8], ds[ds.dec <= 1]
            print("      %s  top2 med %+.3f (n=%d, down %.0f%%)   "
                  "bot2 med %+.3f (n=%d, down %.0f%%)"
                  % (s, t2[col].median(), len(t2), 100 * (t2[col] < 0).mean(),
                     b2[col].median(), len(b2), 100 * (b2[col] < 0).mean()))


if __name__ == "__main__":
    main()
