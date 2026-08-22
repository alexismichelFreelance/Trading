"""Does volume-at-price weighting rescue the ladder in a TRENDING regime?

Plain laddering wins on the 2026 live sample (+3,919 to +9,123) and loses badly
on 88 sessions of 2025 MBO (-6,284 to -20,928). The reason is visible in a
single session: 2025-04-09 ran +496 points, the ladder spent all four slices on
the first four fires at an average of +109, and gave up 386 points. February to
May 2025 is a trending regime with 90-400 point ranges against 40-80 in the 2026
month, so what appeared twice there dominates here.

That is precisely what the volume-at-price weight exists to prevent: on a day
doing real business at the extreme, each slice is smaller, so the ladder cannot
empty itself into a trend. If the idea is sound it should show up here, in the
sample that punishes the unweighted version hardest.

    shr  share of the session's volume SO FAR traded in the extreme decile of
         the range so far, built from the per-minute profile (each minute's
         volume assigned to that minute's close). Uniform is 0.10.
    w    1 / (1 + shr/0.10)

Tops and bottoms are evaluated separately and never share a parameter.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, r"D:\Trading\strategy_lab")
import topsig_2025 as T

PV = 50.0
UNIFORM = 0.10
DEC = 0.10


def causal_share(px, vol, kind):
    """Share of cumulative volume sitting in the extreme decile of the
    cumulative range, evaluated at every minute. Causal by construction."""
    n = len(px)
    out = np.full(n, np.nan)
    runmax = np.maximum.accumulate(px)
    runmin = np.minimum.accumulate(px)
    cv = np.cumsum(vol)
    for i in range(n):
        if i < 5:
            continue
        lo, hi = runmin[i], runmax[i]
        span = hi - lo
        if span <= 0 or cv[i] <= 0:
            continue
        sel = (px[:i + 1] >= hi - DEC * span) if kind == "TOP" \
            else (px[:i + 1] <= lo + DEC * span)
        out[i] = float(vol[:i + 1][sel].sum()) / float(cv[i])
    return out


def evaluate(kind, leg_min, fade_f=0.25, slices=4):
    prior, rows = [], []
    cur = None
    for sym, day, d in T.sessions():
        if sym != cur:
            prior, cur = [], sym
        typ = float(np.median(prior[-T.HIST:])) if len(prior) >= 3 else None
        px = d.px.values
        vol = d.vol.values.astype(float)
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
            shr = causal_share(px, vol, kind)
            op, cl = float(px[0]), float(px[-1])
            sgn = 1.0 if kind == "TOP" else -1.0
            hold = (cl - op) * sgn
            rl = rv = 1.0
            pl = pv = 0.0
            for p, s in zip(px[f], shr[f]):
                if rl > 1e-9:
                    q = min(1.0 / slices, rl)
                    pl += q * (float(p) - op) * sgn
                    rl -= q
                if rv > 1e-9:
                    w = 1.0 / (1.0 + (0.0 if s != s else s) / UNIFORM)
                    q = min((1.0 / slices) * w, rv)
                    pv += q * (float(p) - op) * sgn
                    rv -= q
            pl += rl * (cl - op) * sgn
            pv += rv * (cl - op) * sgn
            rows.append((hold, pl, pv, int(f.sum())))
        if rng > 0:
            prior.append(rng)
    return rows


def main():
    print("  2025 MBO, 88 sessions, slices=4, fade<=0.25   [trending regime]\n")
    print("  %-7s %-5s %8s %10s %10s %12s %9s"
          % ("kind", "leg", "firing", "HOLD $", "LADDER $", "VAP-wtd $", "VAP gain"))
    for kind in ("TOP", "BOTTOM"):
        for lm in [float(x) for x in os.environ.get("LEGS", "0.40,0.50,0.60").split(",")]:
            r = evaluate(kind, lm)
            if not r:
                continue
            h = sum(x[0] for x in r) * PV
            l = sum(x[1] for x in r) * PV
            v = sum(x[2] for x in r) * PV
            print("  %-7s %-5.2f %8d %10.0f %10.0f %12.0f %+9.0f"
                  % (kind, lm, sum(1 for x in r if x[3]), h, l, v, v - l))


if __name__ == "__main__":
    main()
