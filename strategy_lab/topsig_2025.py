"""The top/bottom conjunction on 88 sessions of 2025 Databento MBO.

The live test had 19 sessions of ES and NQ from one month of 2026. This is 88
sessions of ESH5/ESM5 from Feb-May 2025 -- a different year, different
contracts, a different exchange feed, and no overlap whatsoever. Nothing is
re-fitted: the conditions and the parameter grid are exactly those already run
on the live record.

No re-extraction was needed. mbo_minutes.csv already carries buy_v/buy_n and
sell_v/sell_n per minute, so mean print size per side is buy_v/buy_n, and the
signed delta is buy_v - sell_v.

TOPS AND BOTTOMS ARE REPORTED SEPARATELY AND SHARE NOTHING. On the live data
they wanted opposite leg thresholds -- ES tops rising with leg (2731 -> 6225
across 0.30/0.40/0.50) while NQ tops fell (10330 -> 5651) -- so a single value
across the four cells was hiding a real structural difference rather than
finding a common one.
"""
import os
import sys

import numpy as np
import pandas as pd

SRC = r"D:\Trading\strategy_lab\mbo_minutes.csv"
PV = 50.0
HIST = 10


def sessions():
    m = pd.read_csv(SRC)
    m = m[(m["mod"] >= 570) & (m["mod"] < 960)].copy()
    m["delta"] = m.buy_v - m.sell_v
    for sym, g in m.groupby("symbol"):
        for day, d in g.groupby("day", sort=True):
            yield sym, day, d.sort_values("mod").reset_index(drop=True)


def evaluate(kind, leg_min, fade_f, slices=4):
    prior, rows = [], []
    cur_sym = None
    for sym, day, d in sessions():
        if sym != cur_sym:
            prior, cur_sym = [], sym
        typ = float(np.median(prior[-HIST:])) if len(prior) >= 3 else None
        px = d.px.values
        rng = float(px.max() - px.min())
        if typ and typ > 0 and len(px) >= 120:
            conv = d.delta.abs() / d.vol.clip(lower=1)
            live = (d.vol >= d.vol.rolling(30, min_periods=10).median()).values
            fade = (conv <= fade_f * conv.rolling(15, min_periods=5).max()).values
            run = np.maximum.accumulate(px) if kind == "TOP" else np.minimum.accumulate(px)
            oth = np.minimum.accumulate(px) if kind == "TOP" else np.maximum.accumulate(px)
            span = np.maximum(np.abs(run - oth), 1e-9)
            at = np.abs(run - px) <= 0.10 * span
            anc = np.empty(len(px))
            a = px[0]
            for i in range(len(px)):
                if abs(run[i] - px[i]) > 0.25 * span[i]:
                    a = px[i]
                anc[i] = a
            leg = ((px - anc) if kind == "TOP" else (anc - px)) / typ
            f = at & (leg >= leg_min) & live & fade
            ei = int(px.argmax()) if kind == "TOP" else int(px.argmin())
            em = int(d["mod"].iloc[ei])
            offs = [abs(int(mm) - em) for mm in d["mod"].values[f]]
            op, cl = float(px[0]), float(px[-1])
            sgn = 1.0 if kind == "TOP" else -1.0
            hold = (cl - op) * sgn
            rem, pl = 1.0, 0.0
            for p in px[f]:
                if rem <= 1e-9:
                    break
                q = min(1.0 / slices, rem)
                pl += q * (float(p) - op) * sgn
                rem -= q
            pl += rem * (cl - op) * sgn
            rows.append(dict(sym=sym, day=day, n=int(f.sum()), hold=hold,
                             ladder=pl, near5=(min(offs) <= 5) if offs else False,
                             exact=(min(offs) == 0) if offs else False))
        if rng > 0:
            prior.append(rng)
    return rows


def main():
    legs = [float(x) for x in os.environ.get("LEGS", "0.30,0.40,0.50,0.60").split(",")]
    print("  2025 Databento MBO, ESH5+ESM5, slices=4, fade<=0.25\n")
    print("  %-7s %-5s %8s %8s %7s %8s %10s %10s %9s"
          % ("kind", "leg", "sessions", "firing", "fires", "near5", "HOLD $",
             "LADDER $", "gain"))
    for kind in ("TOP", "BOTTOM"):
        for lm in legs:
            r = evaluate(kind, lm, 0.25)
            if not r:
                continue
            fired = [x for x in r if x["n"]]
            h = sum(x["hold"] for x in r) * PV
            l = sum(x["ladder"] for x in r) * PV
            n5 = sum(1 for x in fired if x["near5"])
            print("  %-7s %-5.2f %8d %8d %7d %5d/%-3d %10.0f %10.0f %+9.0f"
                  % (kind, lm, len(r), len(fired), sum(x["n"] for x in fired),
                     n5, len(fired), h, l, l - h))


if __name__ == "__main__":
    main()
