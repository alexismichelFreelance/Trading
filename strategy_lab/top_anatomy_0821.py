"""What was technically happening at the 2026-08-21 high? Tape AND book.

An earlier pass looked at one number -- 5-minute aggressor delta -- saw it was
+742 at the high, and concluded buyers were still strong so the turn was not
visible. That was one measurement, not a search, and it was the wrong number.

+742 of buying is only meaningful against the price it BOUGHT. Aggression that
moves price is a move; aggression that moves nothing is being absorbed, and
absorption at a high is a seller standing there filling everybody. The ratio is
the signal, not the numerator.

Computed here, none of it requiring knowledge of the future:

  impact     points of price change per 1,000 contracts of NET aggression,
             rolling. High early in a move, collapsing at its end.
  buy_sz     mean size of buy-aggressor prints vs sell-aggressor prints. Sellers
             getting larger while buyers get smaller is distribution.
  book       resting size on the bid vs the ask within the top levels we record.
             Offers stacking as price rises = supply arriving to meet it.
  soak       contracts traded within a tick of the running session high without
             the high advancing -- absorption, measured directly.

Sources: claude_ticks_live (every print, with aggressor) and claude_depth_live
(resting book). Both are recorded per event; nothing here is modelled.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd
from engine.adapters.questdb import QuestDB

DAY = "2026-08-21"
SYM = "ES"
LO, HI = "15:00:00", "16:30:00"      # 11:00 -> 12:30 ET, high is 11:53:53
q = QuestDB(timeout=600)


def main():
    t = q.df("SELECT ts,price,size,aggressor FROM claude_ticks_live WHERE symbol='"
             + SYM + "' AND ts >= '" + DAY + "T" + LO + ".000000Z' AND ts < '"
             + DAY + "T" + HI + ".000000Z' ORDER BY ts").copy()
    if t.empty:
        print("no ticks")
        return
    t["et"] = pd.to_datetime(t["ts"]).dt.tz_convert("America/New_York")
    t["m"] = t.et.dt.floor("min")
    t["signed"] = t["size"] * t.aggressor

    g = t.groupby("m").agg(px=("price", "last"), hi=("price", "max"),
                           delta=("signed", "sum"), vol=("size", "sum"),
                           n=("size", "size"))
    g["dpx"] = g.px.diff()
    # impact: points per 1000 contracts of NET aggression, over 5-minute blocks
    r_d = g.delta.rolling(5, min_periods=3).sum()
    r_p = g.px.diff(5)
    g["impact"] = np.where(r_d.abs() > 50, r_p / (r_d / 1000.0), np.nan)

    b = t[t.aggressor > 0].groupby("m")["size"].mean().rename("buy_sz")
    s = t[t.aggressor < 0].groupby("m")["size"].mean().rename("sell_sz")
    g = g.join(b).join(s)

    # absorption: volume printed within a tick of the running high
    t["runhi"] = t.price.cummax()
    t["at_hi"] = (t.runhi - t.price) <= 0.25
    g = g.join(t[t.at_hi].groupby("m")["size"].sum().rename("soak")).fillna({"soak": 0})

    # book: resting size bid vs ask
    d = q.df("SELECT ts,side,size FROM claude_depth_live WHERE symbol='" + SYM
             + "' AND ts >= '" + DAY + "T" + LO + ".000000Z' AND ts < '" + DAY
             + "T" + HI + ".000000Z' ORDER BY ts").copy()
    if not d.empty:
        d["et"] = pd.to_datetime(d["ts"]).dt.tz_convert("America/New_York")
        d["m"] = d.et.dt.floor("min")
        bk = d.groupby(["m", "side"])["size"].mean().unstack(fill_value=np.nan)
        bk.columns = ["ask_sz" if c < 0 else "bid_sz" for c in bk.columns]
        g = g.join(bk)
        if "bid_sz" in g and "ask_sz" in g:
            g["b_over_a"] = g.bid_sz / g.ask_sz

    print("%s %s   ticks=%d   depth rows=%d\n" % (SYM, DAY, len(t), len(d)))
    print("  %-6s %9s %7s %7s %8s %8s %8s %7s %8s"
          % ("ET", "price", "delta", "vol", "impact", "buy_sz", "sell_sz",
             "soak", "bid/ask"))
    for m, r in g.iterrows():
        star = "  <== HIGH" if m.strftime("%H:%M") == "11:53" else ""
        print("  %-6s %9.2f %7.0f %7.0f %8s %8.2f %8.2f %7.0f %8s%s"
              % (m.strftime("%H:%M"), r.px, r.delta, r.vol,
                 "-" if np.isnan(r.impact) else "%.2f" % r.impact,
                 r.get("buy_sz", np.nan), r.get("sell_sz", np.nan), r.soak,
                 "-" if "b_over_a" not in g or np.isnan(r.get("b_over_a", np.nan))
                 else "%.2f" % r.b_over_a, star))


if __name__ == "__main__":
    main()
