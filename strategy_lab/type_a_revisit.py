"""Type A, re-tested honestly: push alone, and conviction as a RANK not a level.

WHY THIS REPLACES THE EARLIER TEST. Type A was defined as push >= 15 points and
conv < 0.12, both read off 2025 Databento MBO. Applying that 0.12 to the 2026
NT8 record was invalid: the two feeds do not produce the same quantity.

    conviction per RTH minute     p25     median    p75
    2025 MBO                     0.033    0.070    0.119
    2026 NT8 live                0.056    0.120    0.205

Databento emits one trade record per aggressor order (1,237 per minute); NT8
consolidates, so prints are fewer and larger and net delta is a bigger share of
volume. `conv < 0.12` therefore selects the bottom 75% of 2025 minutes and the
bottom 50% of 2026 ones -- different populations, and the conclusion drawn from
comparing them ("conviction inverts between years") does not follow.

WHAT THIS DOES INSTEAD
  * conviction is used as a PERCENTILE RANK computed inside each dataset, so
    "low conviction" means the same thing on both sides of the comparison
  * push is normalised by the day's range, so ES and NQ are comparable and no
    points threshold is carried across instruments
  * push-alone is tested separately, because if it carries the result then the
    conviction half was never doing anything and Type A is simpler than claimed

Tops and bottoms stay separate throughout. Outcome is the move away over 30
minutes as a fraction of the day's range; negative always means reversed.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd

A25 = r"D:\Trading\strategy_lab\extreme_shapes2.csv"
A26 = r"D:\Trading\strategy_lab\shapes_2026.csv"


def prep(path, label):
    t = pd.read_csv(path)
    need = {"v_in", "conv", "rev", "kind"}
    if not need.issubset(t.columns):
        raise SystemExit("%s missing columns" % path)
    t = t.dropna(subset=["v_in", "conv", "rev"]).copy()
    t["src"] = label
    # push over the last 10 minutes as a fraction of the day's range.
    # v_in is points-per-minute over 10 minutes, so push10 = v_in * 10.
    if "rng" in t.columns:
        t["push_n"] = (t.v_in * 10.0) / t.rng
    else:
        t["push_n"] = np.nan
    return t


def report(t, label):
    print("\n" + "=" * 84)
    print("%s   n=%d" % (label, len(t)))
    print("=" * 84)
    for kind in ("TOP", "BOTTOM"):
        k = t[t.kind == kind].copy()
        if len(k) < 8:
            continue
        k["conv_pct"] = k.conv.rank(pct=True)
        k["push_pct"] = k.push_n.rank(pct=True) if k.push_n.notna().any() else np.nan
        base = k.rev.median()
        print("\n  %s  n=%d   population median %+0.3f" % (kind, len(k), base))

        # --- conviction as a rank, within this dataset ---
        lo = k[k.conv_pct <= 0.33]
        hi = k[k.conv_pct >= 0.67]
        print("    conviction LOW  third n=%2d  %+0.3f" % (len(lo), lo.rev.median()))
        print("    conviction HIGH third n=%2d  %+0.3f" % (len(hi), hi.rev.median()))

        # --- push alone ---
        if k.push_n.notna().any():
            pl = k[k.push_pct <= 0.33]
            ph = k[k.push_pct >= 0.67]
            print("    push       LOW  third n=%2d  %+0.3f" % (len(pl), pl.rev.median()))
            print("    push       HIGH third n=%2d  %+0.3f" % (len(ph), ph.rev.median()))

            # --- the two combined, the original Type A idea ---
            both = k[(k.push_pct >= 0.67) & (k.conv_pct <= 0.33)]
            pushonly = k[(k.push_pct >= 0.67) & (k.conv_pct > 0.33)]
            print("    PUSH-high + CONV-low  n=%2d  %+0.3f   <- Type A, rank form"
                  % (len(both), both.rev.median() if len(both) else np.nan))
            print("    PUSH-high, conv not low n=%2d %+0.3f   <- does conv add anything?"
                  % (len(pushonly), pushonly.rev.median() if len(pushonly) else np.nan))
            if len(both):
                for r in both.sort_values("rev").itertuples():
                    print("        %s %-5s push %+0.3f of range  conv %.3f (pct %.2f)"
                          "  -> %+0.3f" % (r.day, r.sym, r.push_n, r.conv,
                                           r.conv_pct, r.rev))


def main():
    t25 = prep(A25, "2025 MBO ES")
    t26 = prep(A26, "2026 live ES+NQ")
    report(t25, "2025  ESH5/ESM5, Databento MBO")
    report(t26, "2026  ES+NQ, NT8 live")


if __name__ == "__main__":
    main()
