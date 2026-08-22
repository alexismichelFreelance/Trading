"""The top/bottom conjunction, across every session with tick data.

THE SIGNAL, unchanged from the 2026-08-21 observation and deliberately kept as a
CONJUNCTION rather than a score. An earlier attempt averaged these into a
weighted number and it could not tell 11:53 from 11:04 -- averaging let a strong
reading on one component paper over a missing one, and percentile-ranking
saturated exactly where a top lives.

  CONTEXT   price is at the session extreme (within 10% of the day's range),
            AND the run into it is >= LEG of a TYPICAL day's range.
            Measured against a typical day, NOT against today's range so far:
            early in a session the day's own range is tiny, so a 4-point move
            reads as half the day. On 2026-08-21 that error made the signal
            fire at 11:06 (leg 0.09 of a typical day, but 0.5 of the range so
            far) and give back the whole edge.
  LIVE      this minute's volume >= the median of the last 30 minutes.
            Participation has NOT dried up -- an absorbed extreme, not a drift.
  FADE      |net delta| / volume <= FADE x its own peak over the last 15
            minutes. Conviction has collapsed relative to the move that
            produced this price: heavy two-sided trade, nobody winning.

All three must hold. Everything is computed from the session's own recent
minutes plus a range scale from PRIOR sessions -- nothing looks ahead.

Bottoms are the mirror in every respect and are reported separately, because
tops and bottoms proved to be different populations: over 69 extremes of 2025
MBO, five features separated bottoms while nothing separated tops by more than
0.083, and V-spikes are 46% of bottoms against 18% of tops.

Prints one row per session: how often it fired, and where each fire sat relative
to the actual extreme. The false alarms are shown, not summarised.
"""
import os
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd
from engine.adapters.questdb import QuestDB

Q = QuestDB(timeout=1800)
FADE = float(os.environ.get("FADE", "0.25"))
HIST = 10
RTH_LO, RTH_HI = 570, 960


def tape(sym):
    t = Q.df("SELECT ts,price,size,aggressor FROM claude_ticks_live "
             "WHERE symbol='" + sym + "' ORDER BY ts").copy()
    if t.empty:
        return pd.DataFrame()
    t["et"] = pd.to_datetime(t["ts"]).dt.tz_convert("America/New_York")
    t["day"] = t.et.dt.strftime("%Y-%m-%d")
    t["mod"] = t.et.dt.hour * 60 + t.et.dt.minute
    t = t[(t["mod"] >= RTH_LO) & (t["mod"] < RTH_HI)]
    t["signed"] = t["size"] * t.aggressor
    return t.groupby(["day", "mod"]).agg(
        px=("price", "last"), vol=("size", "sum"),
        delta=("signed", "sum")).reset_index()


def session(d, typ, kind, leg_min):
    d = d.sort_values("mod").reset_index(drop=True)
    px = d.px.values
    n = len(px)
    if n < 120 or not typ or typ <= 0:
        return None
    conv = (d.delta.abs() / d.vol.clip(lower=1))
    live = (d.vol >= d.vol.rolling(30, min_periods=10).median()).values
    fade = (conv <= FADE * conv.rolling(15, min_periods=5).max()).values
    run = np.maximum.accumulate(px) if kind == "TOP" else np.minimum.accumulate(px)
    other = np.minimum.accumulate(px) if kind == "TOP" else np.maximum.accumulate(px)
    span = np.maximum(np.abs(run - other), 1e-9)
    at_ext = (np.abs(run - px) <= 0.10 * span)
    anchor = np.empty(n)
    a = px[0]
    for i in range(n):
        if abs(run[i] - px[i]) > 0.25 * span[i]:
            a = px[i]
        anchor[i] = a
    leg = ((px - anchor) if kind == "TOP" else (anchor - px)) / typ
    fire = at_ext & (leg >= leg_min) & live & fade
    ei = int(px.argmax()) if kind == "TOP" else int(px.argmin())
    em = int(d["mod"].iloc[ei])
    offs = [int(m) - em for m in d["mod"].values[fire]]
    return dict(n=int(fire.sum()), offs=offs, em=em, ext=float(px[ei]),
                leg_at_ext=float(leg[ei]), fire_at_ext=bool(fire[ei]))


def run_all(sym, kind, leg_min, verbose=True):
    g = tape(sym)
    if g.empty:
        return []
    days = sorted(g.day.unique())
    prior, out = [], []
    for day in days:
        d = g[g.day == day]
        typ = float(np.median(prior[-HIST:])) if len(prior) >= 3 else None
        r = session(d, typ, kind, leg_min)
        if r:
            r["day"] = day
            out.append(r)
        rng = float(d.px.max() - d.px.min())
        if rng > 0:
            prior.append(rng)
    return out


def table(sym, kind, leg_min):
    rows = run_all(sym, kind, leg_min)
    if not rows:
        print("  no data for %s %s" % (sym, kind))
        return
    print("\n%s %s   leg>=%.2f of a typical day, fade<=%.2f   %d sessions"
          % (sym, kind, leg_min, FADE, len(rows)))
    print("  %-12s %10s %7s %7s %7s   %s"
          % ("day", "extreme", "at", "fires", "leg@ext", "offsets from the extreme (min)"))
    hit = near = silent = 0
    for r in rows:
        o = sorted(r["offs"])
        if r["n"] == 0:
            silent += 1
        else:
            best = min(abs(x) for x in o)
            if best == 0:
                hit += 1
            if best <= 5:
                near += 1
        print("  %-12s %10.2f  %02d:%02d %7d %7.2f   %s"
              % (r["day"], r["ext"], r["em"] // 60, r["em"] % 60, r["n"],
                 r["leg_at_ext"], o[:9] if o else "-"))
    fired = len(rows) - silent
    tot = sum(r["n"] for r in rows)
    print("  --- %d of %d sessions fired (%d silent), %d fires total, "
          "%.1f per firing session" % (fired, len(rows), silent, tot,
                                       tot / fired if fired else 0))
    print("  --- a fire landed WITHIN 5 MIN of the extreme on %d of %d firing "
          "sessions (exactly on it: %d)" % (near, fired, hit))


if __name__ == "__main__":
    for lm in [float(x) for x in os.environ.get("LEGS", "0.30,0.40").split(",")]:
        for sym in os.environ.get("SYMS", "ES,NQ").split(","):
            for kind in os.environ.get("KINDS", "TOP,BOTTOM").split(","):
                table(sym, kind, lm)
