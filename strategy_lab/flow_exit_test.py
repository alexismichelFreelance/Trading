"""Would exiting on the FLOW TURN have beaten the exits we actually used?

Same entries. Every closed leg the sleeves really took, re-exited on a rule that
watches aggression instead of price give-back, and compared against what the
sleeve actually booked.

THE RULE, stated so it can be wrong:
  hold while aggression agrees with the position. Exit at the first second where
  the trailing `win` seconds of aggressor delta oppose the position AND stay
  opposed for `hold` consecutive seconds. Never exit later than the sleeve did.

WHY THIS IS THE HONEST TEST. On 2026-08-21 the flow turned 0s (NQ) and 28s (ES)
after the day's high, at $0 and $12 of give-back, on moves that then fell $1,985
and $1,000. That looks decisive -- but it was measured in a window already known
to contain the high. The number that decides whether the rule is real is how
often it fires while the move is NOT over, which costs the whole remaining run.
That cost is included here automatically: an early exit books whatever price was
at the moment it fired, and the trades where the sleeve's own exit did better
show up as losses in the comparison.

NO THRESHOLD FITTING. `win` and `hold` are swept over a coarse grid and the WHOLE
grid is printed. If only one cell works, that is a fitted number and not a
finding.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd
from engine.adapters.questdb import QuestDB

PV = {"ES": 50.0, "NQ": 20.0}
q = QuestDB(timeout=600)


def legs():
    f = q.df("SELECT ts,symbol,sleeve,side,qty,price,tag FROM claude_paper_fills "
             "WHERE ts >= '2026-07-30' ORDER BY sleeve, ts").copy()
    f = f[f.sleeve != "__ingest_probe__"]
    f["et"] = pd.to_datetime(f["ts"]).dt.tz_convert("America/New_York")
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
                    if ent is not None:
                        out.append(dict(sleeve=sleeve, sym=sym, dir=sgn,
                                        entry=cost, t0=ent, t1=r.et, tag=r.tag,
                                        booked=n * (r.price - cost) * sgn * PV.get(sym, 50.0)))
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


def flow(sym):
    d = q.df("SELECT ts,pxc,adelta FROM claude_sec_live WHERE symbol='" + sym
             + "' AND pxc > 0 AND ts >= '2026-07-30' ORDER BY ts").copy()
    d["et"] = pd.to_datetime(d["ts"]).dt.tz_convert("America/New_York")
    return d.set_index("et").sort_index()


def main():
    L = legs()
    if L.empty:
        print("no legs")
        return
    F = {s: flow(s) for s in L.sym.unique()}

    print("%d closed legs, %s -> %s\n"
          % (len(L), L.t0.min().strftime("%Y-%m-%d"), L.t1.max().strftime("%Y-%m-%d")))
    print("  %-6s %-6s %5s %12s %12s %12s %7s"
          % ("win_s", "hold_s", "n", "sleeve exit", "flow exit", "difference", "better"))

    for win in (60, 120, 300):
        for hold in (15, 30, 60):
            tot_s = tot_f = 0.0
            better = 0
            n = 0
            for r in L.itertuples():
                d = F.get(r.sym)
                if d is None:
                    continue
                w = d.loc[r.t0:r.t1]
                if len(w) < win + hold + 5:
                    continue
                n += 1
                roll = w.adelta.rolling(win, min_periods=win).sum().values
                px = w.pxc.values
                opp = np.where(np.isnan(roll), False, (roll * r.dir) < 0)
                # first index where `hold` consecutive seconds all oppose
                exit_i = None
                run = 0
                for i in range(len(opp)):
                    run = run + 1 if opp[i] else 0
                    if run >= hold:
                        exit_i = i
                        break
                fpx = px[exit_i] if exit_i is not None else px[-1]
                fpnl = (fpx - r.entry) * r.dir * PV.get(r.sym, 50.0)
                tot_s += r.booked
                tot_f += fpnl
                better += int(fpnl > r.booked)
            if n:
                print("  %-6d %-6d %5d %12.0f %12.0f %12.0f %6.0f%%"
                      % (win, hold, n, tot_s, tot_f, tot_f - tot_s,
                         100.0 * better / n))


if __name__ == "__main__":
    main()
