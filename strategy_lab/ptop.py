"""P(top) -- a VISIBLE score, printed minute by minute. Not a statistic.

THE TARGET, stated as a trade rather than an aggregate:

    2026-08-21  ES:trendjoin_narrow  LONG from 11:02
                peak 11:53 -- the exact minute of the RTH high -- with $988 open
                booked $125 at 12:32. Gave back 87%.

The entry was right, the hold was right, the exit rule did what it says. What is
missing is any awareness that 11:53 was the top. That is the only thing this
computes.

FOUR COMPONENTS, each observably true at that high, each measured against the
SESSION'S OWN history so there are no absolute constants and nothing per
instrument to tune:

  spent  day's range so far / median range of the prior 10 sessions.
         ES stood at 0.86 at 11:53 -- most of a normal day already produced.
  ext    price - session VWAP, in volume-weighted session sigmas. 2.79 at the
         high, having peaked at 3.18 twelve minutes earlier: a higher price at
         LOWER extension, because VWAP was rising into it.
  fade   aggression dying into the extreme: this minute's |net delta| / volume
         against the largest value of the last 15 minutes. The high minute
         printed conviction 0.016 on 1.31x median volume -- heavy two-sided
         trade with no net winner.
  flip   size dominance changing hands: mean BUY print / mean SELL print,
         against its own median over the prior 15 minutes. 15.00 vs 6.33 at
         11:51; 11.11 vs 13.68 at 11:53 -- the first inversion of the up-leg.
  cont   CONTAINMENT -- the counterweight to `spent`, and the component without
         which this whole score is the bug it is meant to fix. A day that has
         produced a normal range is only "done" if it is not still expanding.
         Measured at the moment sessions first reach 0.80 of a typical range
         (strategy_lab/range_break.py), over 44 sessions of 2025 MBO and 27 of
         2026 live, four features predict MORE expansion afterwards and all
         four replicate in direction across both datasets:

             early in the day   tod    gap 0.64 / 0.40
             fast pace          pace   gap 0.64 / 0.39
             wide opening range or_rel gap 0.59 / 0.39
             high volume        vol    gap 0.61 / 0.26
             FEW retracements   legs   gap 0.54 / 0.31

         so containment is their mirror: late, slow, quiet, already-swung.
         Without it, `spent` alone fired at 10:03 on 2026-07-31 and the day ran
         another 350 minutes and 82 points.

         Depth of retracement is deliberately NOT used: it flips sign between
         datasets. Prior-day-level piercing and the overnight gap are excluded
         for the same reason.

Each becomes a 0..1 EXPANDING percentile within the session so far, so the value
at minute m uses only minutes <= m. P(top) is their average, reported when price
is near the session high.

It is a SCORE, not a calibrated probability. It is named for what it ranks;
whether it earns the word "probability" is a later question than whether it
ranks anything at all.
"""
import os
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd
from engine.adapters.questdb import QuestDB

Q = QuestDB(timeout=900)


def minutes(sym, day):
    t = Q.df("SELECT ts,price,size,aggressor FROM claude_ticks_live WHERE symbol='"
             + sym + "' AND ts >= '" + day + "' AND ts < '" + day
             + "T23:59:00.000000Z' ORDER BY ts").copy()
    if t.empty:
        return pd.DataFrame()
    t["et"] = pd.to_datetime(t["ts"]).dt.tz_convert("America/New_York")
    t["mod"] = t.et.dt.hour * 60 + t.et.dt.minute
    t = t[(t["mod"] >= 570) & (t["mod"] < 960)]
    t["signed"] = t["size"] * t.aggressor
    g = t.groupby("mod").agg(px=("price", "last"), hi=("price", "max"),
                             lo=("price", "min"), vol=("size", "sum"),
                             delta=("signed", "sum")).reset_index()
    bs = t[t.aggressor > 0].groupby("mod")["size"].mean().rename("buy_sz")
    ss = t[t.aggressor < 0].groupby("mod")["size"].mean().rename("sell_sz")
    return g.join(bs, on="mod").join(ss, on="mod")


def baselines(sym, day, n=10):
    """Median RTH range AND median per-minute volume of the prior `n` sessions.
    Both are needed: the range to scale `spent`, the volume to say whether today
    is busier than usual, which is one of the expansion signals."""
    d = Q.df("SELECT ts,pxc FROM claude_sec_live WHERE symbol='" + sym
             + "' AND pxc > 0 AND ts < '" + day + "' ORDER BY ts").copy()
    if d.empty:
        return None, None
    d["et"] = pd.to_datetime(d["ts"]).dt.tz_convert("America/New_York")
    d["day"] = d.et.dt.strftime("%Y-%m-%d")
    d["mod"] = d.et.dt.hour * 60 + d.et.dt.minute
    d = d[(d["mod"] >= 570) & (d["mod"] < 960)]
    r = d.groupby("day").pxc.agg(["min", "max"])
    r = (r["max"] - r["min"])
    r = r[r > 0].tail(n)
    v = Q.df("SELECT ts,avol FROM claude_sec_live WHERE symbol='" + sym
             + "' AND ts < '" + day + "' ORDER BY ts").copy()
    vb = None
    if not v.empty:
        v["et"] = pd.to_datetime(v["ts"]).dt.tz_convert("America/New_York")
        v["day"] = v.et.dt.strftime("%Y-%m-%d")
        v["mod"] = v.et.dt.hour * 60 + v.et.dt.minute
        v = v[(v["mod"] >= 570) & (v["mod"] < 960)]
        pm = v.groupby(["day", "mod"]).avol.sum().groupby("day").mean().tail(n)
        vb = float(pm.median()) if len(pm) else None
    return (float(r.median()) if len(r) else None), vb


def typical(sym, day, n=10):
    d = Q.df("SELECT ts,pxc FROM claude_sec_live WHERE symbol='" + sym
             + "' AND pxc > 0 AND ts < '" + day + "' ORDER BY ts").copy()
    if d.empty:
        return None
    d["et"] = pd.to_datetime(d["ts"]).dt.tz_convert("America/New_York")
    d["day"] = d.et.dt.strftime("%Y-%m-%d")
    d["mod"] = d.et.dt.hour * 60 + d.et.dt.minute
    d = d[(d["mod"] >= 570) & (d["mod"] < 960)]
    r = d.groupby("day").pxc.agg(["min", "max"])
    r = r["max"] - r["min"]
    r = r[r > 0].tail(n)
    return float(r.median()) if len(r) else None


def erank(s, min_n=10):
    """Expanding percentile: value at i vs all values up to and including i."""
    a = s.values.astype(float)
    out = np.full(len(a), np.nan)
    for i in range(len(a)):
        if i + 1 < min_n or not np.isfinite(a[i]):
            continue
        w = a[:i + 1]
        w = w[np.isfinite(w)]
        if len(w) >= min_n:
            out[i] = float((a[i] >= w).mean())
    return pd.Series(out, index=s.index)


def build(g, typ, kind="TOP", vbase=None):
    s = 1.0 if kind == "TOP" else -1.0
    v = g.vol.clip(lower=1).astype(float)
    cv = v.cumsum()
    g["vwap"] = (g.px * v).cumsum() / cv
    g["sd"] = np.sqrt(np.maximum(((g.px ** 2) * v).cumsum() / cv - g.vwap ** 2, 1e-12))
    g["ext"] = s * (g.px - g.vwap) / g.sd
    g["rhi"] = g.hi.cummax()
    g["rlo"] = g.lo.cummin()
    g["spent"] = (g.rhi - g.rlo) / typ
    g["conv"] = g.delta.abs() / g.vol.clip(lower=1)
    g["fade"] = 1.0 - g.conv / g.conv.rolling(15, min_periods=5).max()
    dom = g.buy_sz / g.sell_sz.replace(0, np.nan)
    g["dom"] = dom if kind == "TOP" else 1.0 / dom
    g["flip"] = g.dom.rolling(15, min_periods=5).median() / g.dom
    # ── CONTAINMENT: is the day still expanding, or is it finished? ──────
    n = np.arange(1, len(g) + 1)
    g["tod"] = g["mod"] - 570
    # range produced per minute so far, against a typical session's pace
    g["pace"] = ((g.rhi - g.rlo) / n) / (typ / 390.0)
    # counter-moves against the day's direction, deeper than 25% of the range
    up = (g.px.values >= g.px.values[0])
    runx = np.maximum.accumulate(g.px.values) if kind == "TOP"         else np.minimum.accumulate(g.px.values)
    draw = (runx - g.px.values) if kind == "TOP" else (g.px.values - runx)
    span = np.maximum((g.rhi - g.rlo).values, 1e-9)
    deep = (draw > 0.25 * span).astype(int)
    g["legs"] = np.concatenate([[0], np.cumsum(np.diff(deep) == 1)])
    g["volr"] = (g.vol.cumsum() / n) / vbase if vbase else np.nan
    # containment is the MIRROR of the expansion signals: late, slow, quiet,
    # already-swung. Each is an expanding rank so it is comparable to the rest.
    r_tod = erank(g["tod"])
    r_slow = 1.0 - erank(g["pace"])
    r_legs = erank(g["legs"])
    parts = [r_tod, r_slow, r_legs]
    if vbase:
        parts.append(1.0 - erank(g["volr"]))
    g["cont"] = pd.concat(parts, axis=1).mean(axis=1)

    for c in ("spent", "ext", "fade", "flip"):
        g["r_" + c] = erank(g[c])
    g["r_cont"] = g["cont"]
    g["score"] = g[["r_spent", "r_ext", "r_fade", "r_flip",
                    "r_cont"]].mean(axis=1)
    span = (g.rhi - g.rlo).clip(lower=1e-9)
    near = ((g.rhi - g.px) if kind == "TOP" else (g.px - g.rlo)) <= 0.15 * span
    g["P"] = np.where(near, g.score, np.nan)
    return g


def show(sym, day, kind="TOP", lo_m=11 * 60 + 10, hi_m=12 * 60 + 20):
    typ, vbase = baselines(sym, day)
    g = minutes(sym, day)
    if g.empty or not typ:
        print("no data for %s %s" % (sym, day))
        return None
    g = build(g, typ, kind, vbase)
    ex_i = int(g.hi.values.argmax()) if kind == "TOP" else int(g.lo.values.argmin())
    em = int(g["mod"].iloc[ex_i])
    print("%s %s  %s   typical prior range %.2f   extreme %.2f at %02d:%02d\n"
          % (sym, day, kind, typ,
             g.hi.iloc[ex_i] if kind == "TOP" else g.lo.iloc[ex_i],
             em // 60, em % 60))
    print("  %-6s %9s %6s %6s %6s %6s %6s  %6s"
          % ("ET", "price", "spent", "ext", "fade", "flip", "cont", "P"))
    for r in g.itertuples():
        m = int(r.mod)
        if m < lo_m or m > hi_m:
            continue
        f = lambda x: "-" if x != x else "%.2f" % x            # noqa: E731
        print("  %02d:%02d  %9.2f %6.2f %6.2f %6s %6s %6s  %6s%s"
              % (m // 60, m % 60, r.px, r.spent, r.ext, f(r.fade), f(r.flip),
                 f(r.cont), f(r.P),
                 "  <== EXTREME" if int(r.Index) == ex_i else ""))
    v = g.P.dropna()
    at = g.P.iloc[ex_i]
    if len(v) and at == at:
        print("\n  P at the extreme minute: %.2f    rank among the %d "
              "near-the-extreme minutes: %.0f%%"
              % (at, len(v), 100 * (at >= v).mean()))
        print("  the 5 highest-P minutes of the session:")
        for r in g.dropna(subset=["P"]).nlargest(5, "P").itertuples():
            m = int(r.mod)
            print("     %02d:%02d  px %9.2f  P %.2f   (%+d min from the extreme)"
                  % (m // 60, m % 60, r.px, r.P, m - em))
    return g


if __name__ == "__main__":
    show(os.environ.get("SYM", "ES"), os.environ.get("DAY", "2026-08-21"),
         os.environ.get("KIND", "TOP"))
