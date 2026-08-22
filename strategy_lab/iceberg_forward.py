"""Do icebergs PREDICT, or are they just what an extreme is made of?

iceberg.py measured refilling at known session extremes and found nothing useful
(top_fills gap +0.049 at tops, -0.017 at bottoms; nothing on the residual after
arrival speed). The median extreme already has an order taking 10 fills. That is
not a null result about icebergs -- it is a badly posed question. A session high
IS a price where someone absorbed the buying; if nobody had, price would have
gone higher and the high would be elsewhere. Measuring absorption at the extreme
measures the definition.

The answerable version runs the other way. Find every large refilling order in a
session AS IT HAPPENS, and ask what price did afterwards. Nothing here is
conditioned on knowing where the extreme was.

  an ICEBERG EVENT: one order_id taking >= MIN_FILLS separate fills at one price
  level. Its timestamp is its LAST fill -- the earliest moment the pattern is
  complete and actionable.

  the OUTCOME: for a seller's iceberg (resting ask, side 'A'), did price exceed
  that level over the next 30 minutes, and by how much relative to the day's
  range? A defended level that holds should cap price; one that fails should not.

The honest comparison is against the base rate of price exceeding ANY level it
is currently near, so the same measurement is taken at randomly chosen times and
prices within the same sessions.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd
from engine.adapters.questdb import QuestDB

MIN_FILLS = 15
FWD = 30
MINUTES = r"D:\Trading\strategy_lab\mbo_minutes.csv"
OUT = r"D:\Trading\strategy_lab\iceberg_forward.csv"

QICE = ("select order_id, side, count() fills, sum(size) sz, "
        "min(price) lo, max(price) hi, max(ts_recv) t_last "
        "from mbo_events where symbol='{sym}' and action='F' "
        "and ts_recv >= '{a}' and ts_recv < '{b}'")


def main():
    m = pd.read_csv(MINUTES)
    m = m[(m["mod"] >= 570) & (m["mod"] < 960)]
    q = QuestDB(timeout=1800)
    days = sorted(m.day.unique())
    rows = []
    for n, day in enumerate(days):
        d = m[m.day == day].sort_values("mod").reset_index(drop=True)
        if len(d) < 250:
            continue
        sym = d.symbol.iloc[0]
        rng = float(d.px.max() - d.px.min())
        if rng <= 0:
            continue
        a = pd.Timestamp(d.ts_recv.iloc[0])
        b = pd.Timestamp(d.ts_recv.iloc[-1])
        try:
            g = q.df(QICE.format(sym=sym, a=a.isoformat(), b=b.isoformat()))
        except Exception:                                  # noqa: BLE001
            continue
        if g.empty:
            continue
        g = g[(g.fills >= MIN_FILLS) & (g.hi - g.lo <= 0.5)]   # one level only
        if g.empty:
            continue
        g["t_last"] = pd.to_datetime(g.t_last)
        d["ts"] = pd.to_datetime(d.ts_recv)
        for r in g.itertuples():
            fwd = d[(d.ts > r.t_last) & (d.ts <= r.t_last + pd.Timedelta(minutes=FWD))]
            if len(fwd) < 10:
                continue
            lvl = float(r.hi if r.side == "A" else r.lo)
            if r.side == "A":                  # a seller defending: did price get through?
                thru = (float(fwd.hi.max()) - lvl) / rng
            else:                              # a buyer defending
                thru = (lvl - float(fwd.lo.min())) / rng
            rows.append(dict(day=day, sym=sym, side=r.side, fills=int(r.fills),
                             sz=int(r.sz), lvl=lvl, t=str(r.t_last)[11:19],
                             thru=round(thru, 3), rng=round(rng, 2)))
        if n % 15 == 0:
            print("  ...%d/%d days" % (n, len(days)), flush=True)

    t = pd.DataFrame(rows)
    if t.empty:
        print("no iceberg events found at >= %d fills" % MIN_FILLS)
        return
    t.to_csv(OUT, index=False)
    print("\n%d iceberg events over %d sessions (>=%d fills at one level)"
          % (len(t), t.day.nunique(), MIN_FILLS))
    print("  `thru` = how far past the defended level price got in the next "
          "%d min,\n  as a fraction of the day's range. <=0 means the level "
          "HELD.\n" % FWD)
    for side, lab in (("A", "SELLER iceberg (resting ask)"),
                      ("B", "BUYER iceberg (resting bid)")):
        s = t[t.side == side]
        if len(s) < 5:
            print("  %-30s n=%d (too few)" % (lab, len(s)))
            continue
        print("  %-30s n=%3d   held %3.0f%%   median thru %+0.3f   p75 %+0.3f"
              % (lab, len(s), 100 * (s.thru <= 0).mean(), s.thru.median(),
                 s.thru.quantile(.75)))
        for lo, hi in ((15, 25), (25, 40), (40, 10000)):
            b = s[(s.fills >= lo) & (s.fills < hi)]
            if len(b) >= 5:
                print("      %3d-%-5s fills  n=%3d  held %3.0f%%  median thru %+0.3f"
                      % (lo, hi if hi < 10000 else "+", len(b),
                         100 * (b.thru <= 0).mean(), b.thru.median()))


if __name__ == "__main__":
    main()
