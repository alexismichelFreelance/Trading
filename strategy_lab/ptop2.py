"""P(top) with CROSS-SESSION normalisation. Replaces the expanding-rank version.

WHY THE FIRST VERSION WAS BROKEN. It ranked every component against its own
history within the session. `spent` (range so far / typical), `tod` and `legs`
are all NON-DECREASING, so an expanding percentile of them is identically 1.000
-- verified: r_spent had exactly one distinct value across 381 minutes. Two of
five components were constants, and the range-expansion work was mathematically
absent from a score that appeared to contain it.

THE FIX. A session-level quantity is normalised against the SAME QUANTITY AT THE
SAME MINUTE on the prior sessions. "Is the range unusual for 11:53?" is the
question that matters and it needs no scaling constants -- the comparison set
supplies the scale.

COMPONENTS, all causal, all price/volume only so the live feed can produce them.
Each is a 0..1 percentile of today against the prior `HIST` sessions at that
minute, then averaged.

  spent   range so far / typical range           high = day already delivered
  ext     price - VWAP in session sigmas         high = stretched
  fade    1 - conv / its own 15-min max          high = aggression dying
  flip    dominance vs its own 15-min median     high = size changing hands
  cont    CONTAINMENT, the counterweight to `spent`: without it a spent day
          reads as finished even while it is still expanding, which is exactly
          how the bare 0.80 trigger fired at 10:03 on 2026-07-31 and watched
          the day run another 350 minutes and 82 points. Built from the four
          expansion signals that replicated across 2025 MBO and 2026 live
          (tod, pace, volume, legs -- gaps 0.64/0.40, 0.64/0.39, 0.61/0.26,
          0.54/0.31), mirrored so that late + slow + quiet + already-swung
          reads as contained.

Depth of retracement, prior-day-level piercing and the overnight gap are all
excluded: each flips sign between the two datasets.
"""
import os
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd
from engine.adapters.questdb import QuestDB

Q = QuestDB(timeout=900)
HIST = 10
RTH_LO, RTH_HI = 570, 960


def load(sym, upto):
    """Per-minute tape for every session up to and including `upto`."""
    t = Q.df("SELECT ts,price,size,aggressor FROM claude_ticks_live WHERE symbol='"
             + sym + "' AND ts < '" + upto + "T23:59:00.000000Z' ORDER BY ts").copy()
    if t.empty:
        return pd.DataFrame()
    t["et"] = pd.to_datetime(t["ts"]).dt.tz_convert("America/New_York")
    t["day"] = t.et.dt.strftime("%Y-%m-%d")
    t["mod"] = t.et.dt.hour * 60 + t.et.dt.minute
    t = t[(t["mod"] >= RTH_LO) & (t["mod"] < RTH_HI)]
    t["signed"] = t["size"] * t.aggressor
    g = t.groupby(["day", "mod"]).agg(
        px=("price", "last"), hi=("price", "max"), lo=("price", "min"),
        vol=("size", "sum"), delta=("signed", "sum")).reset_index()
    bs = t[t.aggressor > 0].groupby(["day", "mod"])["size"].mean().rename("buy_sz")
    ss = t[t.aggressor < 0].groupby(["day", "mod"])["size"].mean().rename("sell_sz")
    return g.join(bs, on=["day", "mod"]).join(ss, on=["day", "mod"])


def session_feats(d, typ, kind="TOP"):
    """The raw per-minute quantities for ONE session. No normalisation here."""
    d = d.sort_values("mod").reset_index(drop=True)
    s = 1.0 if kind == "TOP" else -1.0
    v = d.vol.clip(lower=1).astype(float)
    cv = v.cumsum()
    n = np.arange(1, len(d) + 1)
    vwap = (d.px * v).cumsum() / cv
    sd = np.sqrt(np.maximum(((d.px ** 2) * v).cumsum() / cv - vwap ** 2, 1e-12))
    rhi, rlo = d.hi.cummax(), d.lo.cummin()
    conv = d.delta.abs() / d.vol.clip(lower=1)
    dom = d.buy_sz / d.sell_sz.replace(0, np.nan)
    if kind != "TOP":
        dom = 1.0 / dom
    runx = np.maximum.accumulate(d.px.values) if kind == "TOP" \
        else np.minimum.accumulate(d.px.values)
    draw = (runx - d.px.values) if kind == "TOP" else (d.px.values - runx)
    span = np.maximum((rhi - rlo).values, 1e-9)
    deep = (draw > 0.25 * span).astype(int)
    legs = np.concatenate([[0], np.cumsum(np.diff(deep) == 1)])
    # LEG EXTENT -- how far the current move has travelled from the last
    # meaningful pullback, in typical-range units. Without this the score
    # cannot tell 11:04 from 11:53 on 2026-08-21: both read spent~0.77,
    # ext 1.00, cont~0.65, yet one is 15.25 points below the day's high and
    # the other IS the high. A top needs a move to top OUT of, and the leg is
    # that move. It is also the one quantity that replicated 4 of 4 across
    # 2025 MBO and 2026 live -- fast-arriving extremes reverse about twice as
    # hard as slow ones.
    px = d.px.values
    anchor = np.empty(len(px))
    a = px[0]
    for i in range(len(px)):
        # the anchor resets whenever price closes back through 25% of the
        # day's range against the leg -- i.e. a real pullback happened
        if kind == "TOP":
            if px[i] < a:
                a = px[i] if (runx[i] - px[i]) > 0.25 * span[i] else a
        else:
            if px[i] > a:
                a = px[i] if (px[i] - runx[i]) > 0.25 * span[i] else a
        anchor[i] = a
    leg = ((px - anchor) if kind == "TOP" else (anchor - px)) / typ

    out = pd.DataFrame({
        "mod": d["mod"].values, "px": d.px.values, "leg": leg,
        "spent": ((rhi - rlo) / typ).values,
        "ext": (s * (d.px - vwap) / sd).values,
        "fade": (1.0 - conv / conv.rolling(15, min_periods=5).max()).values,
        "flip": (dom.rolling(15, min_periods=5).median() / dom).values,
        "tod": d["mod"].values - RTH_LO,
        "pace": (((rhi - rlo) / n) / (typ / 390.0)).values,
        "volp": (d.vol.cumsum() / n).values,
        "legs": legs,
        "near": (((rhi - d.px) if kind == "TOP" else (d.px - rlo))
                 <= 0.15 * span).values})
    return out


def typicals(g, days):
    """Median RTH range of the prior sessions, per session, causally."""
    out = {}
    prior = []
    for d in days:
        out[d] = float(np.median(prior[-HIST:])) if len(prior) >= 3 else None
        k = g[g.day == d]
        r = float(k.hi.max() - k.lo.min())
        if r > 0:
            prior.append(r)
    return out


def score(sym, day, kind="TOP"):
    g = load(sym, day)
    if g.empty:
        return None
    days = sorted(g.day.unique())
    if day not in days:
        return None
    typ = typicals(g, days)
    feats = {}
    for d in days:
        t = typ.get(d)
        if t and t > 0:
            feats[d] = session_feats(g[g.day == d], t, kind)
    if day not in feats:
        return None
    prior_days = [d for d in days if d < day and d in feats][-HIST:]
    today = feats[day].copy()

    # cross-session percentile: today's value at minute m against the same
    # minute on the prior sessions. The comparison set supplies the scale, so
    # no constants are needed and monotone quantities stay informative.
    cols = ["spent", "ext", "fade", "flip", "tod", "pace", "volp",
            "legs", "leg"]
    ref = {c: {} for c in cols}
    for d in prior_days:
        f = feats[d].set_index("mod")
        for c in cols:
            for m, v in f[c].items():
                if v == v:
                    ref[c].setdefault(m, []).append(float(v))
    # TAIL-LIVING quantities are NOT percentile-ranked. `leg` and `ext` both
    # saturate at 1.00 once today exceeds every prior session at that minute --
    # on 2026-08-21 r_leg hit 1.00 at 11:36 and stayed there through the high at
    # 11:53, so the raw leg rose 0.57 -> 0.82 while its rank said nothing. A top
    # lives in the tail and a percentile compresses exactly that. These get a
    # soft ratio against the prior sessions' median instead: 0.5 when today is
    # typical, rising without a ceiling. Scale-free, no constants, order kept.
    SOFT = set()          # tried and REJECTED -- see the note above. The soft
                          # ratio compressed every score below 0.78, so the
                          # trigger either fired at 11:04 or never fired at all.
                          # Percentile saturation is a real defect but this was
                          # not the cure; kept here so it is not retried blind.
    for c in cols:
        vals = []
        for m, v in zip(today["mod"], today[c]):
            pool = ref[c].get(int(m), [])
            if v != v or len(pool) < 3:
                vals.append(np.nan)
            elif c in SOFT:
                med = float(np.median(pool))
                d = abs(v) + abs(med)
                vals.append(float(abs(v) / d) if d > 1e-9 else np.nan)
            else:
                vals.append(float((v >= np.array(pool)).mean()))
        today["r_" + c] = vals

    # containment = the MIRROR of the four expansion signals
    today["r_cont"] = np.nanmean(np.c_[today.r_tod, 1.0 - today.r_pace,
                                       1.0 - today.r_volp, today.r_legs], axis=1)
    today["P"] = np.nanmean(np.c_[today.r_spent, today.r_ext, today.r_fade,
                                  today.r_flip, today.r_cont, today.r_leg],
                            axis=1)
    today.loc[~today.near.astype(bool), "P"] = np.nan
    today["prior_sessions"] = len(prior_days)
    return today


def show(sym, day, kind="TOP", lo_m=11 * 60 + 20, hi_m=12 * 60 + 10):
    t = score(sym, day, kind)
    if t is None or t.P.notna().sum() == 0:
        print("no usable data for %s %s %s" % (sym, day, kind))
        return None
    ex_i = int(t.px.values.argmax()) if kind == "TOP" else int(t.px.values.argmin())
    em = int(t["mod"].iloc[ex_i])
    print("%s %s %s   %d prior sessions   extreme %.2f at %02d:%02d\n"
          % (sym, day, kind, t.prior_sessions.iloc[0], t.px.iloc[ex_i],
             em // 60, em % 60))
    print("  %-6s %9s %6s %6s %6s %6s %6s %6s   %6s"
          % ("ET", "price", "spent", "leg", "ext", "fade", "flip", "cont", "P"))
    f = lambda x: "-" if x != x else "%.2f" % x                # noqa: E731
    for r in t.itertuples():
        m = int(r.mod)
        if m < lo_m or m > hi_m:
            continue
        print("  %02d:%02d  %9.2f %6s %6s %6s %6s %6s %6s   %6s%s"
              % (m // 60, m % 60, r.px, f(r.r_spent), f(r.r_leg), f(r.r_ext),
                 f(r.r_fade), f(r.r_flip), f(r.r_cont), f(r.P),
                 "  <== EXTREME" if int(r.Index) == ex_i else ""))
    v = t.P.dropna()
    at = t.P.iloc[ex_i]
    if at == at:
        print("\n  P at the extreme: %.2f   rank among %d near-extreme minutes: %.0f%%"
              % (at, len(v), 100 * (at >= v).mean()))
    print("  5 highest-P minutes of the session:")
    for r in t.dropna(subset=["P"]).nlargest(5, "P").itertuples():
        m = int(r.mod)
        print("     %02d:%02d  px %9.2f  P %.2f   (%+d min from the extreme)"
              % (m // 60, m % 60, r.px, r.P, m - em))
    return t


if __name__ == "__main__":
    show(os.environ.get("SYM", "ES"), os.environ.get("DAY", "2026-08-21"),
         os.environ.get("KIND", "TOP"))
