"""Iceberg detection at session extremes, from order-level fills.

A native CME iceberg shows a small displayed size and replenishes it each time
it is hit. In MBO that appears as one order_id taking MANY separate fills. At
the 2025-03-04 ESH5 high the top absorbing order took 33 fills for 82 contracts
in 2.3 seconds, and a second took 19 fills over 12.8 seconds, while the average
resting order in that 30-minute window took 1.1 fills for 1.5 contracts across
1,249 distinct orders. That is a seller who kept putting it back, at the high of
the day, and it is invisible in trade data -- a tape shows only that 82 lots
traded, not that one participant supplied all of them.

CONVENTION, verified rather than assumed: in this feed `T` carries the AGGRESSOR
side and `F` the RESTING side. In the window above, T side='B' totalled 105
contracts and F side='A' totalled exactly 105 -- aggressive buys consuming
resting asks. So the absorbing side is 'A' at a top and 'B' at a bottom.

PER EXTREME, in a +/-5 minute window at prices within 1 point of it:
    orders      distinct resting orders hit on the absorbing side
    fills       fill events
    filled      contracts absorbed
    top_fills   fill count of the single most-hit order  <- the iceberg measure
    top_size    contracts absorbed by that one order
    conc        top_size / filled: how much of the absorption was one player

ONLY 2025 CAN BE TESTED. The NT8 live feed carries no order ids, so this cannot
be validated out-of-sample on 2026 the way arrival speed was. Any result here is
single-dataset and must be read that way -- but if it is strong it is an
argument for sourcing order-level data live, which is a decision worth having
evidence for.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd
from engine.adapters.questdb import QuestDB

SHAPES = r"D:\Trading\strategy_lab\extreme_shapes2.csv"
MIN = r"D:\Trading\strategy_lab\mbo_minutes.csv"
OUT = r"D:\Trading\strategy_lab\iceberg.csv"
BAND = 1.0
WIN = 5

# totals, uncapped -- an earlier version used `limit 400` ordered by size,
# which truncated `filled`/`orders` on most extremes and could miss the very
# thing being hunted: an order with MANY SMALL fills.
QTOT = ("select count_distinct(order_id) orders, count() fills, sum(size) sz "
        "from mbo_events where symbol='{sym}' and action='F' and side='{side}' "
        "and price >= {lo} and price <= {hi} "
        "and ts_recv >= '{a}' and ts_recv < '{b}'")
# the most-REFILLED order, ranked by fill COUNT, which is the iceberg signature
QTOP = ("select order_id, count() fills, sum(size) sz from mbo_events "
        "where symbol='{sym}' and action='F' and side='{side}' "
        "and price >= {lo} and price <= {hi} "
        "and ts_recv >= '{a}' and ts_recv < '{b}' "
        "order by fills desc limit 1")


def main():
    sh = pd.read_csv(SHAPES)
    m = pd.read_csv(MIN)
    m = m[(m["mod"] >= 570) & (m["mod"] < 960)]
    q = QuestDB(timeout=900)
    rows = []
    for n, e in enumerate(sh.itertuples()):
        d = m[(m.day == e.day) & (m.symbol == e.sym)].sort_values("mod")
        if d.empty:
            continue
        i = int(d.hi.values.argmax()) if e.kind == "TOP" else int(d.lo.values.argmin())
        r0 = d.iloc[i]
        px = float(r0.hi if e.kind == "TOP" else r0.lo)
        t = pd.Timestamp(r0.ts_recv)
        side = "A" if e.kind == "TOP" else "B"
        kw = dict(sym=e.sym, side=side, lo=px - BAND, hi=px + BAND,
                  a=(t - pd.Timedelta(minutes=WIN)).isoformat(),
                  b=(t + pd.Timedelta(minutes=WIN)).isoformat())
        try:
            tot = q.df(QTOT.format(**kw))
            top1 = q.df(QTOP.format(**kw))
        except Exception:                                   # noqa: BLE001
            continue
        if tot.empty or top1.empty or not tot.sz.iloc[0]:
            continue
        filled = float(tot.sz.iloc[0])
        top = top1.iloc[0]
        rows.append(dict(day=e.day, sym=e.sym, kind=e.kind,
                         orders=int(tot.orders.iloc[0]),
                         fills=int(tot.fills.iloc[0]),
                         filled=int(filled),
                         top_fills=int(top.fills),
                         top_size=int(top.sz),
                         conc=round(top.sz / filled, 3) if filled else None,
                         rev=e.rev, v_in=e.v_in, rng=e.rng, dwell=e.dwell))
        if n % 15 == 0:
            print("  ...%d/%d" % (n, len(sh)), flush=True)
    t = pd.DataFrame(rows)
    t.to_csv(OUT, index=False)
    t["push_n"] = (t.v_in * 10.0) / t.rng
    print("\n%d extremes with order-level data -> %s\n" % (len(t), OUT))

    for kind in ("TOP", "BOTTOM"):
        k = t[t.kind == kind].copy()
        if len(k) < 10:
            continue
        print("=== %s  n=%d   median rev %+0.3f ===" % (kind, len(k), k.rev.median()))
        print("   median: orders %.0f  fills %.0f  filled %.0f  top_fills %.0f  "
              "conc %.2f" % (k.orders.median(), k.fills.median(),
                             k.filled.median(), k.top_fills.median(),
                             k.conc.median()))
        for f in ("top_fills", "top_size", "conc", "filled"):
            med = k[f].median()
            lo, hi = k[k[f] <= med], k[k[f] > med]
            if len(lo) < 4 or len(hi) < 4:
                continue
            print("   %-10s low n=%2d %+0.3f    high n=%2d %+0.3f    gap %+0.3f"
                  % (f, len(lo), lo.rev.median(), len(hi), hi.rev.median(),
                     hi.rev.median() - lo.rev.median()))
        # does it add beyond arrival speed?
        x = k.push_n.rank(pct=True).values
        k["resid"] = k.rev.values - np.polyval(np.polyfit(x, k.rev.values, 1), x)
        med = k.top_fills.median()
        lo, hi = k[k.top_fills <= med], k[k.top_fills > med]
        print("   top_fills on the RESIDUAL after arrival speed: "
              "low %+0.3f   high %+0.3f   gap %+0.3f\n"
              % (lo.resid.median(), hi.resid.median(),
                 hi.resid.median() - lo.resid.median()))


if __name__ == "__main__":
    main()
