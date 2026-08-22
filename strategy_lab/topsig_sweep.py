"""Is leg>=0.40 / fade<=0.25 a plateau or a cliff?

Both numbers were read off a single session (2026-08-21 ES). A threshold taken
from one day is a guess until the surface around it is known: if the result
collapses either side of it, it is fitted; if it sits on a plateau, the level
is incidental and the mechanism is doing the work.

For every cell, across every session with tick data, both instruments, tops and
bottoms:

    sessions   how many of the 19/25 sessions produce any fire at all
    fires      total signals -- the cost side, since each is a scale-out
    near5      of the firing sessions, how many had a fire within 5 minutes of
               the actual extreme
    capture    mean share of the open->extreme move captured by scaling a slice
               on EVERY fire. This is the number that matters operationally:
               it needs no knowledge of which fire is the last one.
"""
import os
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd

sys.path.insert(0, r"D:\Trading\strategy_lab")
import topsig_all as T


def cell(g, kind, leg_min, fade_f):
    days = sorted(g.day.unique())
    prior = []
    nsess = fired = near5 = exact = 0
    fires = 0
    caps = []
    for day in days:
        d = g[g.day == day].sort_values("mod").reset_index(drop=True)
        typ = float(np.median(prior[-T.HIST:])) if len(prior) >= 3 else None
        rng = float(d.px.max() - d.px.min())
        px = d.px.values
        if typ and typ > 0 and len(px) >= 120:
            nsess += 1
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
            k = int(f.sum())
            if k:
                fired += 1
                fires += k
                ei = int(px.argmax()) if kind == "TOP" else int(px.argmin())
                em = int(d["mod"].iloc[ei])
                offs = [abs(int(m) - em) for m in d["mod"].values[f]]
                if min(offs) <= 5:
                    near5 += 1
                if min(offs) == 0:
                    exact += 1
                op, ext = float(px[0]), float(px[ei])
                den = abs(ext - op)
                if den > 1e-9:
                    caps.append(float(np.mean([abs(p - op) / den for p in px[f]])))
        if rng > 0:
            prior.append(rng)
    return dict(sess=nsess, fired=fired, fires=fires, near5=near5, exact=exact,
                cap=float(np.mean(caps)) if caps else float("nan"))


def main():
    legs = [float(x) for x in os.environ.get("LEGS", "0.20,0.30,0.40,0.50,0.60").split(",")]
    fades = [float(x) for x in os.environ.get("FADES", "0.15,0.25,0.40").split(",")]
    for sym in os.environ.get("SYMS", "ES,NQ").split(","):
        g = T.tape(sym)
        if g.empty:
            continue
        for kind in os.environ.get("KINDS", "TOP,BOTTOM").split(","):
            print("\n%s %s" % (sym, kind))
            print("  %-6s %-6s %8s %7s %9s %9s"
                  % ("leg", "fade", "fired/n", "fires", "near5", "capture"))
            for lm in legs:
                for ff in fades:
                    c = cell(g, kind, lm, ff)
                    print("  %-6.2f %-6.2f %5d/%-3d %7d %6d/%-3d %8.0f%%"
                          % (lm, ff, c["fired"], c["sess"], c["fires"],
                             c["near5"], c["fired"], 100 * c["cap"]))


if __name__ == "__main__":
    main()
