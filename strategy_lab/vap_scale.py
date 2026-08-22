"""Scaling weighted by VOLUME-AT-PRICE. Tops and bottoms sized independently.

THE QUESTION each fire has to answer: how unusual is it to be here? An earlier
version answered it in MINUTES -- the share of the session spent at the running
extreme. That is crude, and the two measures disagree: 2026-08-11 traded 13.9%
of its volume in the top decile of its range while spending only 2% of its
minutes at the running high. Price can do a lot of business at a level it does
not linger at, and the market settles in volume, not clock time.

So the weight is built from the session's own volume-at-price profile:

    shr   the share of the day's volume, SO FAR, that has traded in the extreme
          decile of the day's range so far. Uniform would be 0.10. Well below
          that means price is in territory it has not earned -- a poke.
          At or above means real business has been done up (or down) there, the
          level is accepted, and fading it is fighting the day's character.

    w = 1 / (1 + shr / UNIFORM)     smooth, no cutoff, no day-level switch.
        shr = 0.00 -> w 1.00     a genuine poke, take a full slice
        shr = 0.10 -> w 0.50     ordinary, take half a slice
        shr = 0.31 -> w 0.24     2026-08-03, a trend day: take a quarter

TOPS AND BOTTOMS ARE NOT ASSUMED SYMMETRIC. Each side gets its own leg and fade
parameters and its own profile share, computed on its own side of the range.
The same session reads 0.308 on the top side and 0.012 on the bottom, so a
shared weight would be simply wrong. Where the two sides do end up wanting the
same value that is worth noticing, not assuming.
"""
import os
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd
from engine.adapters.questdb import QuestDB

Q = QuestDB(timeout=1800)
PV = {"ES": 50.0, "NQ": 20.0}
UNIFORM = 0.10
DEC = 0.10          # "extreme decile" of the range


def tape(sym):
    t = Q.df("SELECT ts,price,size,aggressor FROM claude_ticks_live WHERE symbol='"
             + sym + "' ORDER BY ts").copy()
    t["et"] = pd.to_datetime(t["ts"]).dt.tz_convert("America/New_York")
    t["day"] = t.et.dt.strftime("%Y-%m-%d")
    t["mod"] = t.et.dt.hour * 60 + t.et.dt.minute
    t = t[(t["mod"] >= 570) & (t["mod"] < 960)]
    t["signed"] = t["size"] * t.aggressor
    return t


def session_minutes(d):
    g = d.groupby("mod").agg(px=("price", "last"), vol=("size", "sum"),
                             delta=("signed", "sum")).reset_index()
    return g


def causal_share(d, g, kind):
    """For each minute: share of volume SO FAR in the extreme decile of the
    range SO FAR. Strictly causal -- minute m uses ticks up to m only."""
    px = d.price.values
    sz = d["size"].values.astype(float)
    mod = d["mod"].values
    order = np.argsort(mod, kind="stable")
    px, sz, mod = px[order], sz[order], mod[order]
    cum_v = np.cumsum(sz)
    runmax = np.maximum.accumulate(px)
    runmin = np.minimum.accumulate(px)
    out = {}
    # walk minute boundaries, recomputing the profile share incrementally
    idx = 0
    for m in g["mod"].values:
        while idx < len(mod) and mod[idx] <= m:
            idx += 1
        if idx < 5:
            out[m] = np.nan
            continue
        lo, hi = runmin[idx - 1], runmax[idx - 1]
        span = hi - lo
        if span <= 0:
            out[m] = np.nan
            continue
        p, s = px[:idx], sz[:idx]
        sel = (p >= hi - DEC * span) if kind == "TOP" else (p <= lo + DEC * span)
        out[m] = float(s[sel].sum()) / float(cum_v[idx - 1])
    return np.array([out[m] for m in g["mod"].values])


def simulate(sym, kind, leg_min, fade_f, slices=4):
    t = tape(sym)
    days = sorted(t.day.unique())
    prior, rows = [], []
    for day in days:
        d = t[t.day == day]
        g = session_minutes(d)
        typ = float(np.median(prior[-10:])) if len(prior) >= 3 else None
        px = g.px.values
        rng = float(px.max() - px.min())
        if typ and typ > 0 and len(px) >= 120:
            conv = g.delta.abs() / g.vol.clip(lower=1)
            live = (g.vol >= g.vol.rolling(30, min_periods=10).median()).values
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
            shr = causal_share(d, g, kind)
            op, cl = float(px[0]), float(px[-1])
            sgn = 1.0 if kind == "TOP" else -1.0
            hold = (cl - op) * sgn
            rem_l = rem_v = 1.0
            pl = pv_ = 0.0
            for p, s in zip(px[f], shr[f]):
                if rem_l > 1e-9:
                    q = min(1.0 / slices, rem_l)
                    pl += q * (float(p) - op) * sgn
                    rem_l -= q
                if rem_v > 1e-9:
                    w = 1.0 / (1.0 + (0.0 if s != s else s) / UNIFORM)
                    q = min((1.0 / slices) * w, rem_v)
                    pv_ += q * (float(p) - op) * sgn
                    rem_v -= q
            pl += rem_l * (cl - op) * sgn
            pv_ += rem_v * (cl - op) * sgn
            rows.append(dict(day=day, n=int(f.sum()), hold=hold, ladder=pl,
                             vap=pv_, shr_end=float(np.nanmax(shr))))
        if rng > 0:
            prior.append(rng)
    return rows


def main():
    print("  slices=4, weight = 1/(1 + shr/0.10)   [tops and bottoms sized separately]\n")
    print("  %-4s %-7s %-5s %-5s %8s %10s %10s %10s"
          % ("sym", "kind", "leg", "fade", "sessions", "HOLD", "LADDER", "VAP-weighted"))
    for sym in ("ES", "NQ"):
        for kind in ("TOP", "BOTTOM"):
            for leg_min, fade_f in ((0.40, 0.25), (0.30, 0.25), (0.50, 0.25)):
                r = simulate(sym, kind, leg_min, fade_f)
                if not r:
                    continue
                pv = PV[sym]
                h = sum(x["hold"] for x in r) * pv
                l = sum(x["ladder"] for x in r) * pv
                v = sum(x["vap"] for x in r) * pv
                print("  %-4s %-7s %-5.2f %-5.2f %8d %10.0f %10.0f %10.0f"
                      % (sym, kind, leg_min, fade_f, len(r), h, l, v))


if __name__ == "__main__":
    main()
