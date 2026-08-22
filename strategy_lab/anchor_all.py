"""CHARACTERISE the day-spent trigger: when does it fire, relative to the high?

The 2026-08-21 anchor: `scale_at 0.80` fired at 11:51:49, two minutes before the
ES high of the day and 2.50 points below it -- the trade taken manually. That is
one day. This asks what it does on ALL of them, not to decide whether it
"generalises" but to describe its behaviour so the failure modes are known.

For every session, for a LONG position, we record the first moment the day's
range reaches `level` of a typical session, and compare it to that session's RTH
high:

    lead      minutes between firing and the high. Positive = fired BEFORE the
              high (early, which is the useful direction); negative = the high
              was already in and the trigger is late.
    below     points between the firing price and the high. Small = it fired
              near the top; large = it left money behind or fired in a dip.
    left      how much of the day's range came AFTER it fired -- what holding
              the other half was still worth.

A trigger that fires 40 minutes before every high is not the same instrument as
one that fires two minutes before one high, and both are worth knowing about
before putting size behind it.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd
from engine.adapters.questdb import QuestDB
from engine.features.day_range import DayRange

q = QuestDB(timeout=900)
RTH_OPEN, RTH_CLOSE = 9 * 60 + 30, 16 * 60


def load(sym):
    d = q.df("SELECT ts,pxc FROM claude_sec_live WHERE symbol='" + sym
             + "' AND pxc > 0 ORDER BY ts").copy()
    d["et"] = pd.to_datetime(d["ts"]).dt.tz_convert("America/New_York")
    d["day"] = d.et.dt.strftime("%Y-%m-%d")
    d["mod"] = d.et.dt.hour * 60 + d.et.dt.minute
    d["ns"] = d.et.astype("int64")
    return d


def main():
    for sym in ("ES", "NQ"):
        d = load(sym)
        dr = DayRange()
        rows = []
        for day, g in d.groupby("day", sort=True):
            g = g.sort_values("ns")
            rth = g[(g["mod"] >= RTH_OPEN) & (g["mod"] < RTH_CLOSE)]
            if len(rth) < 3600:
                for r in g.itertuples():
                    dr.note(r.ns, r.pxc)
                continue
            hi_i = int(rth.pxc.values.argmax())
            hi_px = float(rth.pxc.iloc[hi_i])
            hi_t = rth.et.iloc[hi_i]
            lo_px = float(rth.pxc.min())
            rec = {"day": day, "hi": hi_px, "hi_t": hi_t,
                   "rng": hi_px - lo_px, "typ": None}
            fired = {}
            for r in g.itertuples():
                dr.note(r.ns, r.pxc)
                if r.mod < RTH_OPEN or r.mod >= RTH_CLOSE:
                    continue
                for lev in (0.80, 1.00):
                    k = "s%.0f" % (lev * 100)
                    if k not in fired and dr.extended(lev):
                        fired[k] = (r.et, r.pxc)
                if "fast" not in fired and dr.fast_extreme(1, 0.15):
                    fired["fast"] = (r.et, r.pxc)
            rec["typ"] = dr.typical()
            for k, v in fired.items():
                rec[k + "_t"], rec[k + "_px"] = v
            rows.append(rec)
        t = pd.DataFrame(rows)
        if t.empty:
            continue
        print("\n" + "=" * 88)
        print("%s   %d sessions   (LONG position; 'lead' >0 = fired BEFORE the high)"
              % (sym, len(t)))
        print("=" * 88)
        for k, lab in (("s80", "day spent 0.80"), ("s100", "day spent 1.00"),
                       ("fast", "arrival speed 0.15")):
            c = t.dropna(subset=[k + "_t"]) if (k + "_t") in t else pd.DataFrame()
            if c.empty:
                print("  %-20s never fired" % lab)
                continue
            lead = (c["hi_t"] - c[k + "_t"]).dt.total_seconds() / 60.0
            below = c["hi"] - c[k + "_px"]
            print("  %-20s fired on %2d/%d sessions   median lead %+6.1f min   "
                  "median %5.2f pts below the high   fired BEFORE the high on "
                  "%2d of %d"
                  % (lab, len(c), len(t), lead.median(), below.median(),
                     int((lead > 0).sum()), len(c)))
        print("\n  per session (day-spent 0.80):")
        c = t.dropna(subset=["s80_t"])
        for r in c.itertuples():
            lead = (r.hi_t - r.s80_t).total_seconds() / 60.0
            print("    %s  high %9.2f at %s   fired %s at %9.2f   "
                  "lead %+6.1f min   %6.2f pts below"
                  % (r.day, r.hi, r.hi_t.strftime("%H:%M"),
                     r.s80_t.strftime("%H:%M"), r.s80_px, lead, r.hi - r.s80_px))


if __name__ == "__main__":
    main()
