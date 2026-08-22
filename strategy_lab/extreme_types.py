"""Let the data propose the types. Hierarchical clustering of session extremes.

Everything so far tested structures somebody had already named -- V-spike,
coil break, failed retest. This asks the opposite question: given only the SHAPE
features, do the extremes fall into groups at all, and if so what are they?

Method: Ward linkage on standardised shape features, tops and bottoms clustered
SEPARATELY because the feature screen showed they are not the same population
(five features separate bottoms strongly; nothing separates tops by more than
0.083 on a median split).

THE OUTCOME IS NOT A CLUSTERING INPUT. `rev` and `rev60` are held out entirely
and only reported per cluster afterwards. Otherwise the clusters would be
defined by what happened next, which is circular and would look wonderful.

n is 34 and 35. That is enough to see whether obvious groups exist and far too
few to trust a fine partition, so k is kept small and every member of every
cluster is printed -- the point is to LOOK at them, not to receive a verdict.
"""
import sys

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, linkage

SRC = r"D:\Trading\strategy_lab\extreme_shapes2.csv"
SHAPE = ["v_in", "accel", "run_len", "coil", "marginal", "retest",
         "v_hi", "v_trend", "conv", "dwell", "tod"]


def main():
    t = pd.read_csv(SRC)
    t["retest"] = t.retest.clip(upper=120)        # 999 == "never" would dominate
    for kind in ("TOP", "BOTTOM"):
        k = t[t.kind == kind].dropna(subset=SHAPE + ["rev"]).reset_index(drop=True)
        X = k[SHAPE].values.astype(float)
        X = (X - X.mean(axis=0)) / (X.std(axis=0) + 1e-9)
        Z = linkage(X, method="ward")
        for nk in (3, 4):
            lab = fcluster(Z, nk, criterion="maxclust")
            k["c%d" % nk] = lab
        print("\n" + "=" * 100)
        print("%s   n=%d   overall median rev %+0.3f" % (kind, len(k), k.rev.median()))
        print("=" * 100)
        for nk in (3, 4):
            col = "c%d" % nk
            print("\n  --- %d clusters ---" % nk)
            for c, g in k.groupby(col):
                cen = g[SHAPE].median()
                print("   cluster %d  n=%-3d  rev %+0.3f (60m %+0.3f)   "
                      "v_in %5.2f accel %5.2f coil %5.2f marg %+6.3f "
                      "retest %3.0f v_hi %4.2f conv %.3f dwell %2.0f tod %3.0f"
                      % (c, len(g), g.rev.median(), g.rev60.median(),
                         cen.v_in, cen.accel, cen.coil, cen.marginal,
                         cen.retest, cen.v_hi, cen.conv, cen.dwell, cen.tod))
        # print members of the 3-cluster solution so the shapes can be read
        print("\n  members (3-cluster):")
        for c, g in k.groupby("c3"):
            print("   cluster %d:" % c)
            for r in g.sort_values("rev").itertuples():
                print("      %s %s  v_in %6.2f accel %6.2f coil %6.2f marg %+6.3f "
                      "retest %3.0f v_hi %5.2f conv %.3f dwell %2d tod %3d  -> %+0.3f"
                      % (r.day, r.sym, r.v_in, r.accel, r.coil, r.marginal,
                         r.retest, r.v_hi, r.conv, r.dwell, r.tod, r.rev))


if __name__ == "__main__":
    main()
