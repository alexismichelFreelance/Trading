"""The 2026-08-21 high, second by second. How early was the turn knowable?

Both ES and NQ printed their RTH high at 11:53:5x ET and reversed. Nine long
sleeves were at maximum profit in that minute and every one of them held through
it; five were still holding at 15:59.

A give-back exit confirms a reversal in the slowest way available -- it waits for
the profit to leave, so it can never be early by construction. The question this
asks is whether the ORDER FLOW said the same thing sooner, and by how much.

For each second around the high we show price, and the aggressor delta rolled
over the trailing 30s and 120s. The comparison that matters:

  price give-back   how far price had already fallen from the high when a
                    give-back rule of a given size would have fired
  flow flip         when cumulative aggression turned over and STAYED over

If the flow turns while price is still at the high, the information was there.
If it turns only after price has already fallen, it is a lagging confirmation
and no better than the trail -- which would be worth knowing too.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import pandas as pd
from engine.adapters.questdb import QuestDB

DAY = "2026-08-21"
q = QuestDB(timeout=300)


def load(sym, lo, hi):
    d = q.df("SELECT ts,pxc,adelta,avol,ntr,bid_cancel,ask_cancel,bid_add,ask_add "
             "FROM claude_sec_live WHERE symbol='" + sym + "' AND pxc > 0 "
             "AND ts >= '" + DAY + "T" + lo + ".000000Z' "
             "AND ts <  '" + DAY + "T" + hi + ".000000Z' ORDER BY ts").copy()
    d["et"] = pd.to_datetime(d["ts"]).dt.tz_convert("America/New_York")
    return d


def main():
    for sym, pv in (("ES", 50.0), ("NQ", 20.0)):
        d = load(sym, "15:35:00", "16:35:00")     # 11:35 -> 12:35 ET
        if d.empty:
            print(sym + ": no data")
            continue
        d = d.reset_index(drop=True)
        hi_i = int(d.pxc.values.argmax())
        hi_px, hi_t = d.pxc.iloc[hi_i], d.et.iloc[hi_i]
        d["d30"] = d.adelta.rolling(30, min_periods=1).sum()
        d["d120"] = d.adelta.rolling(120, min_periods=1).sum()
        d["give"] = (hi_px - d.pxc) * pv          # $ below the high, 1 contract

        print("\n" + "=" * 78)
        print("%s   RTH high %.2f at %s ET" % (sym, hi_px, hi_t.strftime("%H:%M:%S")))
        print("=" * 78)
        print("  %-9s %10s %8s %9s %10s %9s"
              % ("ET", "price", "$below", "delta30s", "delta120s", "avol30s"))
        a30 = d.avol.rolling(30, min_periods=1).sum()
        # every 15s from 4 minutes before to 8 minutes after the high
        for i in range(max(0, hi_i - 240), min(len(d), hi_i + 480), 15):
            r = d.iloc[i]
            mark = "  <== HIGH" if abs(i - hi_i) < 8 else ""
            print("  %-9s %10.2f %8.0f %9.0f %10.0f %9.0f%s"
                  % (r.et.strftime("%H:%M:%S"), r.pxc, r.give, r.d30, r.d120,
                     a30.iloc[i], mark))

        # when did the 120s aggression first turn negative and STAY negative?
        post = d.iloc[hi_i:]
        flip = None
        for i in range(len(post) - 60):
            if post.d120.iloc[i] < 0 and (post.d120.iloc[i:i + 60] < 0).all():
                flip = post.iloc[i]
                break
        print("\n  give-back at the moment flow turned:")
        if flip is not None:
            secs = (flip.et - hi_t).total_seconds()
            print("    flow flipped negative and stayed: %s ET  (+%.0fs after the high)"
                  % (flip.et.strftime("%H:%M:%S"), secs))
            print("    price had given back $%.0f of %.0f-per-contract by then"
                  % ((hi_px - flip.pxc) * pv, pv))
        else:
            print("    never flipped decisively in the window")
        # for reference: how far did it eventually fall?
        low_after = d.iloc[hi_i:].pxc.min()
        print("    the move eventually fell $%.0f below the high in this window"
              % ((hi_px - low_after) * pv))


if __name__ == "__main__":
    main()
