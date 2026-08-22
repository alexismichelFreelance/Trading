"""Exceptional icebergs only, against a MATCHED CONTROL.

iceberg_forward.py found 19,951 events at >=15 fills over 71 sessions -- 281 a
session, one every 1.4 minutes. That is not an iceberg, it is a market maker
doing its job. And it reported that price travelled 0.125 of a daily range past
the "defended" level without ever computing how far price travels past an
ARBITRARY level, so the number meant nothing.

Both are fixed here.

  EXCEPTIONAL. Within each session, an order is only interesting if it is in the
  extreme tail of that session's own distribution -- top 1% by contracts
  absorbed at a single price. Sessions differ in activity, so the tail is
  defined per session rather than by a global constant.

  MATCHED CONTROL. For every iceberg event, a control is taken at the SAME
  timestamp in the SAME session at a level the same distance from the prevailing
  price, but on a randomly chosen offset. Same day, same clock, same distance --
  only the "somebody was defending it" part is removed. The difference between
  the two is the only thing that can be attributed to the iceberg.

  HOLDING is also measured properly. Rather than "did price ever tick past it",
  which almost everything fails, we record how far past, and how long the level
  capped price before giving way.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd

SRC = r"D:\Trading\strategy_lab\iceberg_forward.csv"
MINUTES = r"D:\Trading\strategy_lab\mbo_minutes.csv"
FWD = 30


def main():
    t = pd.read_csv(SRC)
    m = pd.read_csv(MINUTES)
    m = m[(m["mod"] >= 570) & (m["mod"] < 960)].copy()
    m["ts"] = pd.to_datetime(m.ts_recv)
    m["hhmmss"] = m.ts.dt.strftime("%H:%M:%S")

    # --- exceptional = top 1% of that session's absorbed-size distribution ---
    t["sz_pct"] = t.groupby("day").sz.rank(pct=True)
    t["fills_pct"] = t.groupby("day").fills.rank(pct=True)
    print("iceberg events: %d over %d sessions" % (len(t), t.day.nunique()))
    print("size absorbed at one level, per event:  median %d  p90 %d  p99 %d  max %d\n"
          % (t.sz.median(), t.sz.quantile(.90), t.sz.quantile(.99), t.sz.max()))

    rng_state = np.random.default_rng(3)
    ctrl = []
    for day, g in t.groupby("day"):
        d = m[m.day == day]
        if len(d) < 100:
            continue
        rngd = float(d.px.max() - d.px.min())
        for r in g.itertuples():
            row = d[d.hhmmss <= r.t]
            if row.empty:
                continue
            now = float(row.px.iloc[-1])
            dist = abs(r.lvl - now)
            # same distance, random side, same moment
            lvl = now + dist * (1 if rng_state.random() < 0.5 else -1)
            fwd = d[d.hhmmss > r.t].head(FWD)
            if len(fwd) < 10:
                continue
            up = lvl >= now
            thru = ((float(fwd.hi.max()) - lvl) if up
                    else (lvl - float(fwd.lo.min()))) / rngd
            ctrl.append(thru)
    ctrl = pd.Series(ctrl)

    print("  %-34s %7s %8s %8s %8s" % ("", "n", "held%", "median", "p25"))
    print("  %-34s %7d %7.0f%% %+8.3f %+8.3f"
          % ("RANDOM level, matched distance", len(ctrl),
             100 * (ctrl <= 0).mean(), ctrl.median(), ctrl.quantile(.25)))
    for lab, s in (("all icebergs (>=15 fills)", t),
                   ("top 10% by size, per session", t[t.sz_pct >= 0.90]),
                   ("top  1% by size, per session", t[t.sz_pct >= 0.99]),
                   ("top  1% by fill count", t[t.fills_pct >= 0.99])):
        if len(s) < 20:
            continue
        print("  %-34s %7d %7.0f%% %+8.3f %+8.3f"
              % (lab, len(s), 100 * (s.thru <= 0).mean(), s.thru.median(),
                 s.thru.quantile(.25)))
    print("\n  by side, top 1% by size:")
    for side, lab in (("A", "seller"), ("B", "buyer ")):
        s = t[(t.sz_pct >= 0.99) & (t.side == side)]
        if len(s) >= 10:
            print("    %s  n=%4d  held %3.0f%%  median %+0.3f"
                  % (lab, len(s), 100 * (s.thru <= 0).mean(), s.thru.median()))


if __name__ == "__main__":
    main()
