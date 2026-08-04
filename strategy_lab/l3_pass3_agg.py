"""Aggregate pass 3: Q1 activity-matched survival, Q2 timing curve, Q3 side-
specificity."""
import glob
import json
import math

import numpy as np

files = sorted(glob.glob("D:/Trading/strategy_lab/l3_pass3/*.json"))
nights = [json.load(open(f)) for f in files]
print(f"nights: {len(nights)}  cases: {sum(len(n['cases']) for n in nights)}  "
      f"ctrls: {sum(len(n['ctrls']) for n in nights)}")

# rate-match quality
mq = []
for nt in nights:
    for c in nt["ctrls"]:
        cr = c.get("case_rate") or 0
        if cr > 0:
            mq.append(abs(c["rate"] - cr) / cr)
print(f"activity-match quality: median |ctrl_rate-case_rate|/case_rate = {np.median(mq):.0%}")


def sign_test(deltas):
    d = [x for x in deltas if x is not None and x != 0]
    if not d:
        return "n/a"
    pos = sum(1 for x in d if x > 0)
    n = len(d)
    p = sum(math.comb(n, k) for k in range(min(pos, n - pos) + 1)) / 2 ** (n - 1)
    return f"{pos}/{n}+ (p~{min(1.0, p):.3f})"


print("\n== Q1: does the pre-liftoff excess survive ACTIVITY-MATCHED controls? ==")
for metric in ("pre_rc", "pre_rf", "dur_rc", "dur_rf"):
    deltas = []
    for nt in nights:
        if len(nt["cases"]) < 3 or len(nt["ctrls"]) < 3:
            continue
        mc = np.median([c[metric] for c in nt["cases"]])
        mx = np.median([c[metric] for c in nt["ctrls"]])
        deltas.append(mc - mx)
    print(f"  {metric:<7} median night-delta = {np.median(deltas):+7.1f}   "
          f"nights+: {sign_test(deltas)}")

print("\n== Q2: timing curve — receding cancel-clears per 30s bin, [-300s .. 0) ==")
case_bins = np.array([c["bins_rc"] for nt in nights for c in nt["cases"]], float)
ctrl_bins = np.array([c["bins_rc"] for nt in nights for c in nt["ctrls"]], float)
cb = np.median(case_bins, axis=0)
xb = np.median(ctrl_bins, axis=0)
lbl = [f"{-300+30*i:+d}" for i in range(10)]
print("  bin start: " + "  ".join(f"{x:>5}" for x in lbl))
print("  cases    : " + "  ".join(f"{x:>5.1f}" for x in cb))
print("  controls : " + "  ".join(f"{x:>5.1f}" for x in xb))
print("  delta    : " + "  ".join(f"{x:>+5.1f}" for x in cb - xb))

print("\n== Q3: side-specificity — within-case (receding − advancing) pre cancels ==")
for lbl2, key in (("cases", "cases"), ("ctrls", "ctrls")):
    diffs = [c["pre_rc"] - c["pre_ac"] for nt in nights for c in nt[key]]
    med = np.median(diffs)
    pos = sum(1 for d in diffs if d > 0)
    n = sum(1 for d in diffs if d != 0)
    print(f"  {lbl2:<6} n={len(diffs):4d}  median diff = {med:+.1f}  "
          f"positive: {pos}/{n}")
# night-level sign test for cases
deltas = []
for nt in nights:
    if len(nt["cases"]) < 3:
        continue
    deltas.append(np.median([c["pre_rc"] - c["pre_ac"] for c in nt["cases"]]))
print(f"  night-level (cases): median {np.median(deltas):+.1f}  nights+: {sign_test(deltas)}")
