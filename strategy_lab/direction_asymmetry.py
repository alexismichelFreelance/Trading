"""Are sleeves better in one direction than the other -- or is that just drift?

A sleeve whose longs make money and whose shorts lose might be genuinely better
at buying. Or the market may simply have gone up over the sample, in which case
ANY long-biased rule looks good and the finding is about the period, not the
sleeve. Splitting P&L by direction alone cannot tell those apart.

THE BASELINE. For every closed trade we know the instrument, the day, the
direction, and how long it was held. The null is the SAME direction, on the SAME
day, held for the SAME duration, started at a RANDOM time in the session. That
holds the market's drift, the day, and the holding period fixed, and destroys
only the sleeve's timing. If a sleeve's longs beat that null it is choosing good
moments to be long; if they merely match it, it is collecting drift and the
direction split says nothing about skill.

Reported per sleeve and per direction:
  total     realized $, gross
  vs null   the same trades minus the mean of their random-window twins.
            THIS is the number that means something.

The forward record before 2026-07-30 is excluded -- tools/paper_audit.py showed
those rows were priced from a clock that disagreed with the event stream.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd
from engine.adapters.questdb import QuestDB

PV = {"ES": 50.0, "NQ": 20.0}
DRAWS = 400
RTH_LO, RTH_HI = 9 * 60 + 30, 16 * 60
q = QuestDB(timeout=300)


def legs():
    """Closed round-trips from the paper record, average-cost."""
    f = q.df("SELECT ts,symbol,sleeve,side,qty,price FROM claude_paper_fills "
             "WHERE ts >= '2026-07-30' ORDER BY sleeve, ts").copy()
    f = f[~f.sleeve.isin(["__ingest_probe__"])]
    f["et"] = pd.to_datetime(f["ts"]).dt.tz_convert("America/New_York")
    out = []
    for sleeve, g in f.groupby("sleeve"):
        pos, cost, ent = 0, 0.0, None
        sym = g.symbol.iloc[0]
        pv = PV.get(sym, 50.0)
        for r in g.itertuples(index=False):
            qd = int(r.side * r.qty)
            while qd:
                if pos and (pos > 0) != (qd > 0):
                    n = min(abs(qd), abs(pos))
                    sgn = 1 if pos > 0 else -1
                    out.append(dict(sleeve=sleeve, sym=sym, dir=sgn,
                                    usd=n * (r.price - cost) * sgn * pv,
                                    t0=ent, t1=r.et,
                                    mins=(r.et - ent).total_seconds() / 60 if ent else 0.0))
                    pos -= n * sgn
                    qd -= n * (1 if qd > 0 else -1)
                    if pos == 0:
                        ent = None
                else:
                    tot = abs(pos) + abs(qd)
                    cost = (cost * abs(pos) + r.price * abs(qd)) / tot
                    if pos == 0:
                        ent = r.et
                    pos += qd
                    qd = 0
    return pd.DataFrame(out)


def bars():
    b = q.df("SELECT symbol,ts,c FROM claude_bars_live WHERE ts >= '2026-07-29' "
             "ORDER BY symbol, ts").copy()
    b["et"] = pd.to_datetime(b["ts"]).dt.tz_convert("America/New_York")
    b["day"] = b.et.dt.strftime("%Y-%m-%d")
    b["mod"] = b.et.dt.hour * 60 + b.et.dt.minute
    return b


def main():
    L = legs()
    B = bars()
    if L.empty:
        print("no closed legs")
        return
    L["day"] = L.t1.dt.strftime("%Y-%m-%d")

    # index: (sym, day) -> minute-of-day -> close
    idx = {}
    for (s, d), g in B.groupby(["symbol", "day"]):
        gg = g[(g["mod"] >= RTH_LO) & (g["mod"] <= RTH_HI)]
        if len(gg) > 60:
            idx[(s, d)] = (gg["mod"].values, gg.c.values)

    print("=== MARKET over the sample (RTH closes) ===")
    for s in ("ES", "NQ"):
        ds = sorted(d for (ss, d) in idx if ss == s)
        if not ds:
            continue
        first, last = idx[(s, ds[0])][1][0], idx[(s, ds[-1])][1][-1]
        ups = sum(1 for d in ds if idx[(s, d)][1][-1] > idx[(s, d)][1][0])
        print("  %s  %s -> %s   net %+.2f pts (%+.0f$ per contract)   "
              "up-sessions %d/%d" % (s, ds[0], ds[-1], last - first,
                                     (last - first) * PV[s], ups, len(ds)))

    rng = np.random.default_rng(11)
    nulls = []
    for r in L.itertuples():
        key = (r.sym, r.day)
        if key not in idx or r.mins <= 0:
            nulls.append(np.nan)
            continue
        mods, px = idx[key]
        span = max(1, int(round(r.mins)))
        if len(mods) <= span + 1:
            nulls.append(np.nan)
            continue
        starts = rng.integers(0, len(mods) - span - 1, DRAWS)
        moves = px[starts + span] - px[starts]
        nulls.append(float(np.mean(moves)) * r.dir * PV[r.sym])
    L["null"] = nulls
    L["alpha"] = L.usd - L["null"]

    print("\n=== BY SLEEVE AND DIRECTION (gross $; 'vs null' is the real number) ===")
    print("  %-24s %-6s %3s %10s %10s %6s" %
          ("sleeve", "dir", "n", "total", "vs null", "win%"))
    for sleeve, g in L.groupby("sleeve"):
        if len(g) < 4:
            continue
        for d, lab in ((1, "LONG"), (-1, "short")):
            s = g[g["dir"] == d]
            if not len(s):
                continue
            print("  %-24s %-6s %3d %10.0f %10.0f %5.0f%%" %
                  (sleeve, lab, len(s), s.usd.sum(), s.alpha.sum(),
                   100 * (s.usd > 0).mean()))

    print("\n=== TOTALS ===")
    for d, lab in ((1, "LONG"), (-1, "short")):
        s = L[L["dir"] == d]
        print("  %-6s n=%3d  total %+9.0f  vs null %+9.0f  win %3.0f%%  "
              "median hold %3.0f min"
              % (lab, len(s), s.usd.sum(), s.alpha.sum(),
                 100 * (s.usd > 0).mean(), s.mins.median()))
    L.to_csv(r"D:\Trading\strategy_lab\direction_legs.csv", index=False)
    print("\nper-leg detail -> strategy_lab/direction_legs.csv")


if __name__ == "__main__":
    main()
