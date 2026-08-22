"""Flow decay, not flow sign. The repair of flow_exit_test.py.

That version exited when aggression merely OPPOSED the position. Aggression
crosses zero constantly: it fired at a median of 234 seconds into trades the
sleeves held for 4,856, and 29% fired at the first second they were eligible. It
was not a reversal test, it was "exit almost immediately", and it lost $230k
against the sleeves' own exits.

What made 2026-08-21 readable was not the sign. ES aggression ran to +1,011 over
120s, then collapsed to +389 BY the high and went negative 28s later. The
information was in the COLLAPSE RELATIVE TO THE THRUST, not in the zero crossing.

So this judges the flow against its own peak, the way TwoPhaseExit already judges
price against its own pace:

  arm    the position's agreeing aggression reaches its high-water mark
  exit   that measure decays to `frac` of the peak (frac<0 = must actually flip)

`frac` and `win` are swept and the WHOLE grid is printed. One good cell in a
noisy grid is a fitted number, not a finding.

Baseline is what the sleeve actually booked on the same leg -- same entry, same
starting point, only the exit differs. An exit that fires too early gives up the
rest of the run and that cost lands in this comparison automatically.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd
from engine.adapters.questdb import QuestDB

PV = {"ES": 50.0, "NQ": 20.0}
q = QuestDB(timeout=600)


def legs():
    f = q.df("SELECT ts,symbol,sleeve,side,qty,price FROM claude_paper_fills "
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
                        out.append(dict(sleeve=sleeve, sym=sym, dir=sgn, qty=n,
                                        entry=cost, t0=ent, t1=r.et,
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
    F = {s: flow(s) for s in L.sym.unique()}
    cache = {}
    for r in L.itertuples():
        d = F.get(r.sym)
        if d is None:
            continue
        w = d.loc[r.t0:r.t1]
        if len(w) < 400:
            continue
        cache[r.Index] = (w.pxc.values, w.adelta.values, r)

    print("%d legs with enough flow (of %d closed)\n" % (len(cache), len(L)))
    print("  %-6s %-7s %5s %12s %12s %12s %8s %9s"
          % ("win_s", "frac", "n", "sleeve", "flow-decay", "difference",
             "better", "med exit"))

    for win in (120, 300):
        for frac in (0.75, 0.5, 0.25, 0.0, -0.25, -0.5):
            ts_ = tf = 0.0
            better = n = 0
            exits = []
            for key, (px, ad, r) in cache.items():
                roll = pd.Series(ad).rolling(win, min_periods=win).sum().values * r.dir
                peak = -1e18
                ex = None
                for i in range(len(roll)):
                    v = roll[i]
                    if np.isnan(v):
                        continue
                    if v > peak:
                        peak = v
                    if peak > 0 and v <= frac * peak:
                        ex = i
                        break
                fpx = px[ex] if ex is not None else px[-1]
                fp = (fpx - r.entry) * r.dir * r.qty * PV.get(r.sym, 50.0)
                ts_ += r.booked
                tf += fp
                better += int(fp > r.booked)
                exits.append(ex if ex is not None else len(px) - 1)
                n += 1
            if n:
                print("  %-6d %-7.2f %5d %12.0f %12.0f %12.0f %7.0f%% %9.0f"
                      % (win, frac, n, ts_, tf, tf - ts_,
                         100.0 * better / n, float(np.median(exits))))


if __name__ == "__main__":
    main()
