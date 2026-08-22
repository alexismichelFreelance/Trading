"""ANCHOR TEST: would the scale-out have fired at the 2026-08-21 top?

The motivating trade. ES and NQ both printed their RTH high at 11:53. Nine long
sleeves were at maximum profit in that minute; every one held through it, five
were still holding at 15:59, and the book gave back $23,232 of $18,565 in peak
profit. The trade was taken manually and successfully. A mechanism that cannot
catch it is mis-specified, whatever else it scores.

So this replays the real session, second by second, through the ACTUAL objects
the engine would use -- DayRange fed from the recorded tape -- and reports every
minute where each trigger would have fired for a long position:

    scale_at 0.80 / 1.00   the day's range is `level` of a typical session
    scale_push 0.15        price is at a session extreme in our favour having
                           travelled >=0.15 of a typical day's range in 10 min

No re-fitting, no hindsight: the thresholds are the ones already in the roster,
and DayRange's reference is built only from sessions BEFORE this one.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import pandas as pd
from engine.adapters.questdb import QuestDB
from engine.features.day_range import DayRange

DAY = "2026-08-21"
q = QuestDB(timeout=900)


def run(sym):
    d = q.df("SELECT ts,pxc FROM claude_sec_live WHERE symbol='" + sym
             + "' AND pxc > 0 AND ts >= '2026-07-01' AND ts < '"
             + DAY + "T23:59:00.000000Z' ORDER BY ts").copy()
    d["et"] = pd.to_datetime(d["ts"]).dt.tz_convert("America/New_York")
    d["day"] = d.et.dt.strftime("%Y-%m-%d")
    d["ns"] = d.et.astype("int64")

    dr = DayRange()
    fired = {"spent80": None, "spent100": None, "fast": None}
    rows = []
    hi_px, hi_t = -1e18, None
    for r in d.itertuples():
        dr.note(r.ns, r.pxc)
        if r.day != DAY:
            continue
        if r.pxc > hi_px:
            hi_px, hi_t = r.pxc, r.et
        if fired["spent80"] is None and dr.extended(0.80):
            fired["spent80"] = (r.et, r.pxc, dr.used())
        if fired["spent100"] is None and dr.extended(1.00):
            fired["spent100"] = (r.et, r.pxc, dr.used())
        if fired["fast"] is None and dr.fast_extreme(1, 0.15):
            fired["fast"] = (r.et, r.pxc, dr.push(1))
        rows.append((r.et, r.pxc, dr.used(), dr.push(1),
                     dr.at_extreme(1), dr.fast_extreme(1, 0.15)))

    print("\n" + "=" * 72)
    print("%s  %s   typical prior range %.2f   RTH high %.2f at %s"
          % (sym, DAY, dr.typical() or float("nan"), hi_px,
             hi_t.strftime("%H:%M:%S") if hi_t is not None else "-"))
    print("=" * 72)
    for k, lab in (("spent80", "scale_at 0.80 (day spent)"),
                   ("spent100", "scale_at 1.00 (day spent)"),
                   ("fast", "scale_push 0.15 (ARRIVAL SPEED)")):
        v = fired[k]
        if v is None:
            print("  %-34s never fired" % lab)
        else:
            t, px, val = v
            lead = (hi_t - t).total_seconds() / 60.0 if hi_t is not None else 0
            print("  %-34s FIRED %s at %.2f  (%.2f)   %+.0f min vs the high, "
                  "%.2f pts below it"
                  % (lab, t.strftime("%H:%M:%S"), px, val, lead, hi_px - px))

    f = pd.DataFrame(rows, columns=["et", "px", "used", "push", "at_ext", "fast"])
    f = f[(f.et.dt.hour * 60 + f.et.dt.minute >= 11 * 60 + 30) &
          (f.et.dt.hour * 60 + f.et.dt.minute <= 12 * 60 + 10)]
    f["m"] = f.et.dt.strftime("%H:%M")
    g = f.groupby("m").agg(px=("px", "last"), used=("used", "last"),
                           push=("push", "max"), at_ext=("at_ext", "any"),
                           fast=("fast", "any"))
    print("\n  minute by minute around the high:")
    print("    %-6s %9s %7s %7s %7s %6s" %
          ("ET", "price", "used", "push", "at_ext", "FIRE"))
    for m, r in g.iterrows():
        print("    %-6s %9.2f %7.2f %7.3f %7s %6s%s"
              % (m, r.px, r.used if r.used else float("nan"),
                 r.push if r.push is not None else float("nan"),
                 "yes" if r.at_ext else "-", "YES" if r.fast else "-",
                 "   <== HIGH" if m == "11:53" else ""))


if __name__ == "__main__":
    for s in ("ES", "NQ"):
        run(s)
