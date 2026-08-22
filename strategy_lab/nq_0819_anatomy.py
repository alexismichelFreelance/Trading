"""2026-08-19 NQ: one move, understood. Not an average over anything.

NQ opened 29696, fell ~320pt into ~10:11, then ground back up all session to
close 29522. Every continuation sleeve shorted into it: NQ:opendrive shorted
29407 -- thirty points off the low -- and bled until the 15:59 flat.

The question is NOT "do flushes on average revert". It is: on THIS day, at the
low, was there anything in the flow that distinguished the turn from the twenty
minutes of falling before it?

FIELDS (claude_sec_live, one row per second):
  pxc  last price   adelta  signed aggressor volume (lifts minus hits)
  avol total aggressive volume   ntr  trade count
  bid_add/bid_cancel, ask_add/ask_cancel  passive size joining/leaving each side

THE DISTINCTION THAT MATTERS. Price falls because sellers hit bids. That can end
two ways and they look nothing alike in the flow:

  EXHAUSTION  the selling stops. adelta returns toward zero, avol dies. The move
              ends because nobody is left to sell -- only knowable afterwards.
  ABSORPTION  the selling CONTINUES at size but price stops falling. Someone is
              buying passively, taking all of it. adelta stays negative while
              price goes flat or up. This is the footprint of a large buyer who
              is present BEFORE the reversal is visible in price.

Third case: VACUUM -- price falls because bids are CANCELLED rather than hit
(bid_cancel >> bid_add on low avol). Nothing was actually distributed, and those
retrace fast.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import pandas as pd
from engine.adapters.questdb import QuestDB

DAY, SYM = "2026-08-19", "NQ"


def load(sym, day, lo, hi):
    q = QuestDB(timeout=240)
    d = q.df("SELECT ts,pxc,adelta,avol,ntr,bid_cancel,ask_cancel,bid_add,ask_add "
             "FROM claude_sec_live WHERE symbol='" + sym + "' "
             "AND ts>='" + day + "T" + lo + ".000000Z' "
             "AND ts<'" + day + "T" + hi + ".000000Z' ORDER BY ts").copy()
    d["et"] = pd.to_datetime(d["ts"]).dt.tz_convert("America/New_York")
    d = d[d.pxc > 0].copy()
    d["m"] = d.et.dt.strftime("%H:%M")
    return d


def per_minute(d):
    g = d.groupby("m").agg(px=("pxc", "last"), lo=("pxc", "min"), hi=("pxc", "max"),
                           adelta=("adelta", "sum"), avol=("avol", "sum"),
                           ntr=("ntr", "sum"), bcx=("bid_cancel", "sum"),
                           acx=("ask_cancel", "sum"), badd=("bid_add", "sum"),
                           aadd=("ask_add", "sum"))
    g["cum"] = g.adelta.cumsum()
    g["bid_net"] = g.bcx - g.badd
    g["ask_net"] = g.acx - g.aadd
    return g


def main():
    d = load(SYM, DAY, "13:25:00", "16:10:00")
    g = per_minute(d)
    med = g.avol.median()
    print(SYM + " " + DAY + "   " + format(len(d), ",") + " seconds of flow\n")
    print("ET       price     delta   cumdelta   avol   ntr  bidPULL askPULL  read")
    prev = None
    for m, r in g.iterrows():
        if m < "09:30" or m > "11:10":
            prev = r.px
            continue
        read = ""
        if prev is not None:
            if r.px > prev and r.adelta < 0:
                read = "UP on net SELLING   <- ABSORPTION"
            elif r.px < prev and r.adelta > 0:
                read = "DOWN on net BUYING"
            elif r.px < prev and r.bid_net > 0 and r.avol < med:
                read = "vacuum (bids pulled, thin volume)"
        print("%-6s %9.2f %+9.0f %+10.0f %6.0f %5.0f %+8.0f %+8.0f  %s"
              % (m, r.px, r.adelta, r.cum, r.avol, r.ntr, r.bid_net, r.ask_net, read))
        prev = r.px
    w = g.loc["09:30":"11:10"]
    print("\nsession low %.2f printed in the %s minute" % (w.lo.min(), w.lo.idxmin()))


if __name__ == "__main__":
    main()
