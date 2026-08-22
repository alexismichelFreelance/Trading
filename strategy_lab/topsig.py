"""The 2026-08-21 top signature, tested RAW. No ranks, no averaging, no score.

What was observed at the ES high of that day (11:53), in plain numbers:

        price    delta    vol    buy_sz  sell_sz   delta/vol
 11:51  7710.75    +566   1174    15.00     6.33      48%
 11:52  7712.75    +207   1067    10.80     9.56      19%
 11:53  7713.25     +20   1224    11.11    13.68       2%   <== HIGH
 11:54  7712.25    -407    977     6.63    12.81

Two things happen together and neither needs a statistic to see:

  EXHAUSTION  directional conviction collapses while VOLUME DOES NOT. The high
              minute traded 1224 -- the most of those four minutes -- with 2% of
              it net. Heavy two-sided trade, nobody winning. That is a level
              being defended, not a move continuing.
  INVERSION   the average BUY print falls below the average SELL print, having
              been above it. Buyers were 2.4x larger at 11:51 and smaller by
              11:53: big sellers arriving into small buyers.

A previous attempt turned these into percentile ranks and averaged them with
four other components. The ranks saturated, the averaging diluted, and the
signature vanished into a score that could not tell 11:53 from 11:04. This tests
the raw conditions and nothing else.

THREE CONDITIONS, each a direct comparison against the same session's own recent
minutes -- no cross-session fitting, no thresholds carried between instruments:

    live    volume this minute >= the median of the last 30 minutes
            (participation has NOT dried up -- this is what separates an
            absorbed top from a quiet drift)
    fade    |delta|/volume <= FADE_F of the largest value in the last 15
            minutes (conviction has collapsed relative to the move that
            produced this price)
    flip    mean buy print < mean sell print, having been above it on the
            median of the last 10 minutes (dominance has changed hands)

FIRE = live AND fade AND flip. Reported with every minute it fires, so the
false alarms are visible rather than summarised.
"""
import os
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd
from engine.adapters.questdb import QuestDB

Q = QuestDB(timeout=900)
FADE_F = float(os.environ.get("FADE_F", "0.25"))


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
    g = t.groupby("mod").agg(px=("price", "last"), vol=("size", "sum"),
                             delta=("signed", "sum")).reset_index()
    bs = t[t.aggressor > 0].groupby("mod")["size"].mean().rename("buy_sz")
    ss = t[t.aggressor < 0].groupby("mod")["size"].mean().rename("sell_sz")
    return g.join(bs, on="mod").join(ss, on="mod")


def signal(g, kind="TOP"):
    g = g.sort_values("mod").reset_index(drop=True)
    g["conv"] = g.delta.abs() / g.vol.clip(lower=1)
    g["conv_run"] = g.conv.rolling(15, min_periods=5).max()
    g["vol_med"] = g.vol.rolling(30, min_periods=10).median()
    dom = g.buy_sz / g.sell_sz.replace(0, np.nan)
    if kind != "TOP":
        dom = 1.0 / dom
    g["dom"] = dom
    g["dom_prev"] = dom.rolling(10, min_periods=4).median()
    g["live"] = g.vol >= g.vol_med
    g["fade"] = g.conv <= FADE_F * g.conv_run
    g["flip"] = (g.dom < 1.0) & (g.dom_prev > 1.0)
    g["fire"] = g.live & g.fade & g.flip
    return g


def main():
    sym = os.environ.get("SYM", "ES")
    day = os.environ.get("DAY", "2026-08-21")
    kind = os.environ.get("KIND", "TOP")
    g = minutes(sym, day)
    if g.empty:
        print("no data")
        return
    g = signal(g, kind)
    ex = int(g.px.values.argmax()) if kind == "TOP" else int(g.px.values.argmin())
    em = int(g["mod"].iloc[ex])
    print("%s %s %s   extreme %.2f at %02d:%02d   fade factor %.2f\n"
          % (sym, day, kind, g.px.iloc[ex], em // 60, em % 60, FADE_F))
    fires = g[g.fire]
    print("  fired %d times:" % len(fires))
    for r in fires.itertuples():
        m = int(r.mod)
        print("     %02d:%02d  px %9.2f  vol %5.0f  delta %+6.0f  conv %.3f "
              "(run %.3f)  buy %5.2f / sell %5.2f   %+d min from the extreme"
              % (m // 60, m % 60, r.px, r.vol, r.delta, r.conv, r.conv_run,
                 r.buy_sz, r.sell_sz, m - em))
    r = g.iloc[ex]
    print("\n  AT THE EXTREME (%02d:%02d): live=%s fade=%s flip=%s -> fire=%s"
          % (em // 60, em % 60, bool(r.live), bool(r.fade), bool(r.flip),
             bool(r.fire)))
    print("     vol %.0f (median-30 %.0f)   conv %.3f (run max %.3f)   "
          "buy %.2f / sell %.2f" % (r.vol, r.vol_med, r.conv, r.conv_run,
                                    r.buy_sz, r.sell_sz))


if __name__ == "__main__":
    main()
