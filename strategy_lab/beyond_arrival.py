"""What is left AFTER arrival speed? Partial out the one feature that replicates.

Arrival speed -- push into the extreme, equivalently absence of dwell at it --
is the only feature to survive every test: 4 of 4 cells, ~2x separation, across
two years, two feeds, two instrument sets, tops and bottoms. push and dwell
correlate -0.74 to -0.85, so they are one finding and not two.

Everything else must now be judged on what it adds ON TOP of that, not on its
own. A feature that merely correlates with arrival speed will look predictive
and contribute nothing.

METHOD. Within each dataset and each kind, regress `rev` on arrival speed
(rank), take the RESIDUAL, and then split that residual by each remaining
feature. A feature that separates the residual is carrying information arrival
speed does not. A feature that does not is already accounted for.

Reported for both datasets side by side. A feature only matters if it points the
SAME WAY in 2025 and 2026 -- one dataset alone is how conviction fooled me.
"""
import sys

import numpy as np
import pandas as pd

SRC = {"2025": r"D:\Trading\strategy_lab\extreme_shapes2.csv",
       "2026": r"D:\Trading\strategy_lab\shapes_2026.csv"}
FEATS = ["accel", "coil", "marginal", "retest", "v_hi", "v_trend", "conv", "tod"]


def residualise(k):
    """rev with the arrival-speed component removed (linear, on ranks)."""
    x = k.push_n.rank(pct=True).values
    y = k.rev.values
    b = np.polyfit(x, y, 1)
    return y - np.polyval(b, x)


def main():
    res = {}
    for lab, path in SRC.items():
        t = pd.read_csv(path).dropna(subset=["v_in", "rev", "rng"])
        t["push_n"] = (t.v_in * 10.0) / t.rng
        for kind in ("TOP", "BOTTOM"):
            k = t[t.kind == kind].copy()
            if len(k) < 12:
                continue
            k["resid"] = residualise(k)
            for f in FEATS:
                s = k.dropna(subset=[f])
                if len(s) < 12:
                    continue
                med = s[f].median()
                lo, hi = s[s[f] <= med], s[s[f] > med]
                if len(lo) < 4 or len(hi) < 4:
                    continue
                res.setdefault((kind, f), {})[lab] = (
                    lo.resid.median(), hi.resid.median(), len(lo), len(hi))

    for kind in ("TOP", "BOTTOM"):
        print("\n" + "=" * 78)
        print("%s -- residual after removing arrival speed" % kind)
        print("=" * 78)
        print("  %-10s %-24s %-24s %s" % ("feature", "2025 (low / high)",
                                          "2026 (low / high)", "agree?"))
        rows = []
        for f in FEATS:
            v = res.get((kind, f), {})
            if "2025" not in v or "2026" not in v:
                continue
            l5, h5, n5l, n5h = v["2025"]
            l6, h6, n6l, n6h = v["2026"]
            g5, g6 = h5 - l5, h6 - l6
            agree = (g5 > 0) == (g6 > 0)
            rows.append((abs(g5) + abs(g6) if agree else -1, f, l5, h5, l6, h6,
                         agree, g5, g6))
        for _, f, l5, h5, l6, h6, agree, g5, g6 in sorted(rows, reverse=True):
            print("  %-10s %+7.3f / %+7.3f       %+7.3f / %+7.3f      %s  "
                  "(gap %+0.3f vs %+0.3f)"
                  % (f, l5, h5, l6, h6, "YES" if agree else "no ", g5, g6))


if __name__ == "__main__":
    main()
