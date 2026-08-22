"""At the moment a day has produced 0.80 of a typical range -- does it stop, or
does it keep going?

This is not a day-type classifier and not a general question. It is asked at one
instant: the moment the scale-out trigger fires. On 2026-08-21 that instant came
at 11:51 and the high arrived two minutes later -- the day was done. On
2026-07-31 it came at 10:03 and the day ran another 350 minutes and 82 points.
Same trigger, opposite meaning, and nothing currently distinguishes them.

Everything is computed from data available AT T and nothing after it.

  WHEN
    tod         minutes since 09:30. A day that has covered a normal range by
                10:00 has six hours left to cover more; one that takes until
                14:00 does not.
    pace        range so far divided by minutes elapsed, against the typical
                session's range divided by a full session. >1 = running hot.

  HOW IT GOT THERE  (the user's "price action" and "retracements")
    oneway      |price_T - open| / range so far. 1.0 = every point of range was
                in one direction; 0 = it round-tripped.
    deepest     the deepest counter-move so far, as a fraction of range so far.
                A trend that has never retraced more than 20% is a different
                animal from one that has swung twice.
    legs        number of counter-moves exceeding 25% of the range so far.

  PARTICIPATION  (the user's "volume")
    vol_rel     volume per minute so far, against the same clock window
                averaged over the prior sessions. Is today busier than usual?
    vol_trend   volume in the last 30 min against the prior 30.

  LEVELS  (the user's "the way it pierces key levels")
    or_rel      the 09:30-10:00 range against a typical session's range. A wide
                opening range is itself a claim about the day.
    gap_rel     open minus prior RTH close, absolute, against typical range.
    thru_pdh    did price take out the prior session's RTH high (or low, for a
                down day) before T -- and by how much, relative to typical.

  OUTCOME
    extra       range added AFTER T, as a fraction of a typical range. 0 means
                the day was finished; 0.5 means it produced another half
                session's worth after the trigger fired.
"""
import sys

import numpy as np
import pandas as pd

import os
SRC = os.environ.get("RB_SRC", r"D:\Trading\strategy_lab\mbo_minutes.csv")
OUT = r"D:\Trading\strategy_lab\range_break.csv"
LEVEL = 0.80


def sessions(m):
    """Group by SYMBOL AND DAY. Grouping by day alone merged ES and NQ into one
    fictional 21,000-point session that tripped every threshold on the first
    bar. The 2025 file has one contract per day so it never surfaced there."""
    for (sym, day), d in m.groupby(["symbol", "day"], sort=True):
        d = d.sort_values("mod").reset_index(drop=True)
        if len(d) >= 250:
            yield day, d


def main():
    m = pd.read_csv(SRC)
    m = m[(m["mod"] >= 570) & (m["mod"] < 960)].copy()
    rows = []
    for sym_ in sorted(m.symbol.unique()):
        ms = m[m.symbol == sym_]
        prior_rng, prior_hi, prior_lo, prior_close = [], None, None, None
        prior_vol = []      # per-minute volume of prior sessions, this symbol
        rows.extend(_scan(ms, prior_rng, prior_vol))
    _report(pd.DataFrame(rows), m)


def _scan(m, prior_rng, prior_vol):
    prior_hi = prior_lo = prior_close = None
    rows = []
    for day, d in sessions(m):
        typ = float(np.median(prior_rng[-10:])) if len(prior_rng) >= 3 else None
        px = d.px.values
        hi = np.maximum.accumulate(px)
        lo = np.minimum.accumulate(px)
        rr = hi - lo
        if typ and typ > 0:
            idx = np.where(rr >= LEVEL * typ)[0]
            if len(idx):
                i = int(idx[0])
                op = float(px[0])
                so_far = float(rr[i])
                # deepest counter-move and leg count up to T
                run_max = np.maximum.accumulate(px[:i + 1])
                run_min = np.minimum.accumulate(px[:i + 1])
                up = px[i] >= op
                # AGAINST THE DAY'S DIRECTION. The previous version took
                # max(dd_up, dd_dn) over the session, and one of those two IS
                # the session range by construction, so it read 1.00 at every
                # percentile and carried no information at all.
                deepest = (float(np.max(run_max - px[:i + 1])) if up
                           else float(np.max(px[:i + 1] - run_min))) / so_far                     if so_far else np.nan
                draw = (run_max - px[:i + 1]) if up else (px[:i + 1] - run_min)
                legs = int(np.sum(np.diff((draw > 0.25 * so_far).astype(int)) == 1))
                vhere = float(d.vol.iloc[:i + 1].sum()) / max(i + 1, 1)
                vbase = float(np.median(prior_vol[-10:])) if len(prior_vol) >= 3 else np.nan
                v30 = float(d.vol.iloc[max(0, i - 29):i + 1].mean())
                v60 = float(d.vol.iloc[max(0, i - 59):max(1, i - 29)].mean())
                orb = d[(d["mod"] >= 570) & (d["mod"] < 600)]
                thru = np.nan
                if prior_hi is not None and prior_lo is not None:
                    thru = ((float(hi[i]) - prior_hi) if up
                            else (prior_lo - float(lo[i]))) / typ
                rows.append(dict(
                    day=day, sym=d.symbol.iloc[0], typ=round(typ, 2),
                    tod=int(d["mod"].iloc[i]) - 570,
                    pace=round((so_far / max(i + 1, 1)) / (typ / 390.0), 2),
                    oneway=round(abs(px[i] - op) / so_far, 2) if so_far else None,
                    deepest=round(deepest, 2),
                    legs=legs,
                    vol_rel=round(vhere / vbase, 2) if vbase and not np.isnan(vbase) else None,
                    vol_trend=round(v30 / v60, 2) if v60 > 0 else None,
                    or_rel=round((float(orb.px.max() - orb.px.min())) / typ, 2) if len(orb) else None,
                    gap_rel=round(abs(op - prior_close) / typ, 2) if prior_close else None,
                    thru_pdh=round(thru, 2) if thru == thru else None,
                    up=int(up),
                    extra=round((float(rr[-1]) - so_far) / typ, 2)))
        prior_rng.append(float(px.max() - px.min()))
        prior_vol.append(float(d.vol.mean()))
        prior_hi, prior_lo, prior_close = float(px.max()), float(px.min()), float(px[-1])
    return rows


def _report(t, m):
    t.to_csv(OUT, index=False)
    print("%d sessions reached %.0f%% of a typical range   (%d total)\n"
          % (len(t), LEVEL * 100, sum(1 for _ in sessions(m))))
    print("  extra range added AFTER the trigger, in typical-range units:")
    print("    median %+0.2f   p25 %+0.2f   p75 %+0.2f   max %+0.2f"
          % (t.extra.median(), t.extra.quantile(.25), t.extra.quantile(.75),
             t.extra.max()))
    print("    'day was done'  (extra <= 0.15): %2d of %d"
          % (int((t.extra <= 0.15).sum()), len(t)))
    print("    'kept going'    (extra >= 0.50): %2d of %d\n"
          % (int((t.extra >= 0.50).sum()), len(t)))

    feats = ["tod", "pace", "oneway", "deepest", "legs", "vol_rel",
             "vol_trend", "or_rel", "gap_rel", "thru_pdh"]
    print("  %-10s  %-22s %-22s %s" % ("feature", "low half -> extra",
                                       "high half -> extra", "gap"))
    out = []
    for f in feats:
        s = t.dropna(subset=[f])
        if len(s) < 20:
            continue
        med = s[f].median()
        lo, hi = s[s[f] <= med], s[s[f] > med]
        if len(lo) < 8 or len(hi) < 8:
            continue
        out.append((abs(hi.extra.median() - lo.extra.median()), f,
                    lo.extra.median(), hi.extra.median(), len(lo), len(hi)))
    for gap, f, l, h, nl, nh in sorted(out, reverse=True):
        print("  %-10s  %+0.2f (n=%2d)          %+0.2f (n=%2d)          %0.2f"
              % (f, l, nl, h, nh, gap))


if __name__ == "__main__":
    main()
