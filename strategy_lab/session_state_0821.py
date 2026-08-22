"""Today's session in the terms the DECISION was actually made in.

Not a rule, not a trigger, not a backtest. The quantities named as the basis for
exiting near the high on 2026-08-21, computed through the session so they can be
checked against what was actually perceived at the time:

  sigma        distance from session VWAP in volume-weighted standard deviations.
               The 3-sigma band plus a zone was the "potential reversal location".
  range_used   today's high-low so far, as a FRACTION of the median RTH range of
               the prior 10 sessions. This is the "considering the ranges we are
               currently in" quantity -- how much of a normal day has already
               happened. Nothing on the roster computes it.
  from_open    where price sits versus the 09:30 open, in the same units, i.e.
               how much of a normal day's move is currently banked in a position
               held from the open.
  vol_rel      aggressive volume this minute against the median minute so far
               today. "Volume was weakening" is a claim about this.
  d300         aggressor delta over the trailing 5 minutes: who is actually
               paying to move it.

VWAP and its bands are volume-weighted on the session's own prints, cumulative
from 09:30, so every row is computable in real time from what we record.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd
from engine.adapters.questdb import QuestDB

DAY = "2026-08-21"
q = QuestDB(timeout=600)


def prior_ranges(sym, day, n=10):
    d = q.df("SELECT ts,pxc FROM claude_sec_live WHERE symbol='" + sym
             + "' AND pxc > 0 AND ts < '" + day + "' ORDER BY ts").copy()
    if d.empty:
        return None
    d["et"] = pd.to_datetime(d["ts"]).dt.tz_convert("America/New_York")
    d["day"] = d.et.dt.strftime("%Y-%m-%d")
    d["mod"] = d.et.dt.hour * 60 + d.et.dt.minute
    r = d[(d["mod"] >= 570) & (d["mod"] < 960)].groupby("day").pxc.agg(["min", "max"])
    r["rng"] = r["max"] - r["min"]
    r = r[r.rng > 0].tail(n)
    return float(r.rng.median()) if len(r) else None


def main():
    for sym in ("ES", "NQ"):
        med = prior_ranges(sym, DAY)
        d = q.df("SELECT ts,pxc,adelta,avol FROM claude_sec_live WHERE symbol='"
                 + sym + "' AND pxc > 0 AND ts >= '" + DAY
                 + "T13:30:00.000000Z' AND ts < '" + DAY
                 + "T20:00:00.000000Z' ORDER BY ts").copy()
        if d.empty or not med:
            print(sym + ": insufficient data")
            continue
        d["et"] = pd.to_datetime(d["ts"]).dt.tz_convert("America/New_York")
        d = d.reset_index(drop=True)
        v = d.avol.clip(lower=0).astype(float) + 1e-9
        cv = v.cumsum()
        d["vwap"] = (d.pxc * v).cumsum() / cv
        # volume-weighted variance around the running VWAP
        d["v2"] = ((d.pxc ** 2) * v).cumsum() / cv
        d["sd"] = np.sqrt(np.maximum(d.v2 - d.vwap ** 2, 1e-12))
        d["sigma"] = (d.pxc - d.vwap) / d.sd
        d["hi"] = d.pxc.cummax()
        d["lo"] = d.pxc.cummin()
        d["range_used"] = (d.hi - d.lo) / med
        d["from_open"] = (d.pxc - d.pxc.iloc[0]) / med
        d["d300"] = d.adelta.rolling(300, min_periods=30).sum()
        d["vmin"] = d.avol.rolling(60, min_periods=10).sum()
        d["vrel"] = d.vmin / d.vmin.expanding(60).median()

        print("\n" + "=" * 92)
        print("%s  %s   median RTH range of prior 10 sessions = %.2f pts"
              % (sym, DAY, med))
        print("=" * 92)
        print("  %-8s %9s %8s %7s %10s %10s %8s %8s"
              % ("ET", "price", "vwap", "sigma", "range_used", "from_open",
                 "vol_rel", "d300"))
        step = 300           # every 5 minutes
        for i in range(0, len(d), step):
            r = d.iloc[i]
            print("  %-8s %9.2f %8.2f %7.2f %10.2f %10.2f %8.2f %8.0f"
                  % (r.et.strftime("%H:%M"), r.pxc, r.vwap, r.sigma,
                     r.range_used, r.from_open,
                     0.0 if np.isnan(r.vrel) else r.vrel,
                     0.0 if np.isnan(r.d300) else r.d300))
        hi_i = int(d.pxc.values.argmax())
        r = d.iloc[hi_i]
        print("  " + "-" * 88)
        print("  %-8s %9.2f %8.2f %7.2f %10.2f %10.2f %8.2f %8.0f   <== HIGH OF DAY"
              % (r.et.strftime("%H:%M:%S"), r.pxc, r.vwap, r.sigma, r.range_used,
                 r.from_open, 0.0 if np.isnan(r.vrel) else r.vrel,
                 0.0 if np.isnan(r.d300) else r.d300))


if __name__ == "__main__":
    main()
