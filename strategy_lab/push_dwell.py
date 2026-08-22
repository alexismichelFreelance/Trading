"""Are `push` and `dwell` two findings or one? And do they add?

Both survived every test so far -- each separates reversal magnitude about 2:1
in all four cells (2025 MBO tops/bottoms, 2026 NT8 tops/bottoms). But a fast
push into a level mechanically leaves less time to linger at it, so they may be
the same fact measured twice. If they are, one of them should be dropped; if
they are not, the pair should beat either alone.

Both are computed causally: push is the last 10 minutes INTO the extreme as a
fraction of the day's range, dwell counts minutes near the extreme in the 20
minutes BEFORE it (the earlier +/-20 version leaked the outcome).

Everything is by within-dataset percentile rank, because the two feeds do not
produce comparable absolute levels -- Databento emits one record per aggressor
order and NT8 consolidates, which shifted conviction by 1.7x and invalidated an
earlier cross-dataset threshold comparison.
"""
import sys

import numpy as np
import pandas as pd

SRC = {"2025 MBO": r"D:\Trading\strategy_lab\extreme_shapes2.csv",
       "2026 live": r"D:\Trading\strategy_lab\shapes_2026.csv"}


def main():
    for lab, path in SRC.items():
        t = pd.read_csv(path).dropna(subset=["v_in", "dwell", "rev", "rng"])
        t["push_n"] = (t.v_in * 10.0) / t.rng
        print("\n" + "=" * 76)
        print("%s   n=%d" % (lab, len(t)))
        print("=" * 76)
        for kind in ("TOP", "BOTTOM"):
            k = t[t.kind == kind].copy()
            if len(k) < 10:
                continue
            k["p"] = k.push_n.rank(pct=True)
            k["d"] = k.dwell.rank(pct=True)
            r = np.corrcoef(k.push_n, k.dwell)[0, 1]
            print("\n  %s  n=%d   median %+0.3f   corr(push,dwell) %+0.2f"
                  % (kind, len(k), k.rev.median(), r))
            # 2x2: is the pair better than either alone?
            hi_p, lo_p = k.p >= 0.5, k.p < 0.5
            lo_d, hi_d = k.d < 0.5, k.d >= 0.5
            cells = (("push HIGH + dwell LOW  (fast in, no lingering)", hi_p & lo_d),
                     ("push HIGH + dwell high", hi_p & hi_d),
                     ("push low  + dwell LOW ", lo_p & lo_d),
                     ("push low  + dwell high (slow grind, level worked)", lo_p & hi_d))
            for name, sel in cells:
                s = k[sel]
                if len(s):
                    print("    %-50s n=%2d  %+0.3f"
                          % (name, len(s), s.rev.median()))
            print("    %-50s      push %+0.3f / %+0.3f   dwell %+0.3f / %+0.3f"
                  % ("(marginals: high/low)", k[hi_p].rev.median(),
                     k[lo_p].rev.median(), k[lo_d].rev.median(),
                     k[hi_d].rev.median()))


if __name__ == "__main__":
    main()
