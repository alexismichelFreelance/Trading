"""How much profit did each of today's trades HAVE, and how much did it keep?

The complaint is specific and mechanical: a sleeve makes a decent entry, goes
into profit, then watches the profit evaporate and closes negative. This measures
that directly -- for every closed leg, the best unrealised profit it ever held
(MFE), against what it actually booked.

MFE is read from claude_sec_live pxc, one price per second, between the entry
and exit timestamps of the leg itself. No modelling, no assumptions.
"""
import os
import sys

sys.path.insert(0, r"D:\Trading\engine")
import pandas as pd
from engine.adapters.questdb import QuestDB

DAY = os.environ.get("DAY", "2026-08-21")
PV = {"ES": 50.0, "NQ": 20.0}
q = QuestDB(timeout=300)


def legs(day):
    f = q.df("SELECT ts,symbol,sleeve,side,qty,price,tag FROM claude_paper_fills "
             "ORDER BY sleeve, ts").copy()
    f = f[f.sleeve != "__ingest_probe__"]
    f["et"] = pd.to_datetime(f["ts"]).dt.tz_convert("America/New_York")
    f["day"] = f.et.dt.strftime("%Y-%m-%d")
    out = []
    for sleeve, g in f.groupby("sleeve"):
        pos, cost, ent = 0, 0.0, None
        sym = g.symbol.iloc[0]
        for r in g.itertuples(index=False):
            qd = int(r.side * r.qty)
            while qd:
                if pos and (pos > 0) != (qd > 0):
                    n = min(abs(qd), abs(pos))
                    sgn = 1 if pos > 0 else -1
                    if r.day == day and ent is not None:
                        out.append(dict(sleeve=sleeve, sym=sym, dir=sgn,
                                        entry=cost, exitpx=r.price, t0=ent,
                                        t1=r.et, tag=(r.tag or "?"),
                                        usd=n * (r.price - cost) * sgn * PV.get(sym, 50.0)))
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


def main():
    L = legs(DAY)
    if L.empty:
        print("no closed legs on " + DAY)
        return
    px = {}
    for s in L.sym.unique():
        d = q.df("SELECT ts,pxc FROM claude_sec_live WHERE symbol='" + s + "' "
                 "AND pxc > 0 AND ts >= '" + DAY + "' AND ts < '" + DAY
                 + "T23:59:59.000000Z' ORDER BY ts").copy()
        d["et"] = pd.to_datetime(d["ts"]).dt.tz_convert("America/New_York")
        px[s] = d

    rows = []
    for r in L.itertuples():
        d = px.get(r.sym)
        if d is None:
            continue
        w = d[(d.et >= r.t0) & (d.et <= r.t1)]
        if len(w) < 2:
            continue
        pv = PV.get(r.sym, 50.0)
        fav = (w.pxc - r.entry) * r.dir * pv
        rows.append(dict(sleeve=r.sleeve, dir=r.dir, tag=r.tag,
                         t0=r.t0.strftime("%H:%M"), t1=r.t1.strftime("%H:%M"),
                         mins=(r.t1 - r.t0).total_seconds() / 60,
                         mfe=float(fav.max()), booked=r.usd,
                         mfe_at=w.et.iloc[int(fav.values.argmax())].strftime("%H:%M")))
    t = pd.DataFrame(rows)
    t["gaveback"] = t.mfe - t.booked

    print(DAY + " -- what each trade HAD vs what it KEPT (gross $)\n")
    print("  %-24s %-4s %5s %5s %5s %9s %9s %9s  %s"
          % ("sleeve", "dir", "in", "out", "peak", "best$", "booked$",
             "gave back", "exit"))
    for r in t.sort_values("gaveback", ascending=False).itertuples():
        print("  %-24s %-4s %5s %5s %5s %9.0f %9.0f %9.0f  %s"
              % (r.sleeve, "LONG" if r.dir > 0 else "shrt", r.t0, r.t1,
                 r.mfe_at, r.mfe, r.booked, r.gaveback, r.tag))

    print("\n  TOTAL had  %+9.0f$    booked %+9.0f$    gave back %9.0f$"
          % (t.mfe.sum(), t.booked.sum(), t.gaveback.sum()))
    won = t[t.mfe > 0]
    print("  %d of %d legs were in profit at some point; %d of those closed NEGATIVE"
          % (len(won), len(t), int((won.booked < 0).sum())))
    if len(won):
        print("  capture: %.0f%% of peak profit kept" % (100 * t.booked.sum() / t.mfe.sum()))


if __name__ == "__main__":
    main()
