"""Aggregate L3 pass-2 night JSONs: within-night paired deltas + sign tests,
strata (calm vs busy by control coverage), and within-CASE self-contrast
(depth at liftoff vs -300s: each move its own baseline)."""
import glob
import json
import math

import numpy as np

files = sorted(glob.glob("D:/Trading/strategy_lab/l3_pass2/*.json"))
nights = [json.load(open(f)) for f in files]
print(f"nights loaded: {len(nights)}")


def med(rows, path):
    v = []
    for r in rows:
        x = r
        for k in path:
            x = x.get(k) if isinstance(x, dict) else None
            if x is None:
                break
        if x is not None:
            v.append(float(x))
    return float(np.median(v)) if v else None


def sign_test(deltas):
    d = [x for x in deltas if x is not None and x != 0]
    if not d:
        return "n/a"
    pos = sum(1 for x in d if x > 0)
    n = len(d)
    p = sum(math.comb(n, k) for k in range(min(pos, n - pos) + 1)) / 2 ** (n - 1)
    return f"{pos}/{n}+ (p~{min(1.0, p):.2f})"


METRICS = [("pre.cancel", ["pre", "cancel"]), ("pre.fill", ["pre", "fill"]),
           ("dur.cancel", ["dur", "cancel"]), ("dur.fill", ["dur", "fill"]),
           ("depth@0", ["traj", "0"]), ("depth@-300", ["traj", "-300"])]

strata = {"calm": [], "busy": []}
for nt in nights:
    if not nt["cases"]:
        continue
    stratum = "calm" if len(nt["ctrls"]) >= len(nt["cases"]) else "busy"
    strata[stratum].append(nt)

for label, group in strata.items():
    print(f"\n===== stratum {label}: {len(group)} nights, "
          f"{sum(len(n['cases']) for n in group)} cases, "
          f"{sum(len(n['ctrls']) for n in group)} ctrls =====")
    for mname, path in METRICS:
        deltas = []
        for nt in group:
            if len(nt["cases"]) < 3 or len(nt["ctrls"]) < 3:
                continue
            mc = med(nt["cases"], path)
            mx = med(nt["ctrls"], path)
            if mc is not None and mx is not None:
                deltas.append(mc - mx)
        if deltas:
            print(f"  {mname:<12} median night-delta(case-ctrl) = {np.median(deltas):+7.1f}   "
                  f"nights+: {sign_test(deltas)}")

print("\n===== WITHIN-CASE self-contrast: depth@liftoff minus depth@-300s =====")
for label, group in strata.items():
    dd_case, dd_ctrl = [], []
    for nt in group:
        for r in nt["cases"]:
            a, b = r["traj"].get("0"), r["traj"].get("-300")
            if a is not None and b is not None:
                dd_case.append(a - b)
        for r in nt["ctrls"]:
            a, b = r["traj"].get("0"), r["traj"].get("-300")
            if a is not None and b is not None:
                dd_ctrl.append(a - b)
    if dd_case:
        print(f"  {label:<5} cases n={len(dd_case):4d}: median {np.median(dd_case):+.1f}  "
              f"(thinning if negative)   controls n={len(dd_ctrl):4d}: median "
              f"{np.median(dd_ctrl):+.1f}")

print("\n===== quality =====")
ph = [nt["phantom"] / max(1, nt["events"]) for nt in nights]
print(f"phantom-removal rate: median {np.median(ph):.1%}  max {max(ph):.1%}")
nc = [len(nt['ctrls']) for nt in nights]
print(f"controls per night: median {np.median(nc):.0f}; nights with zero ctrls: "
      f"{sum(1 for x in nc if x == 0)}")
