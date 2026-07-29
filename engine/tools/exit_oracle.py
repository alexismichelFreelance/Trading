"""Where IS the best exit? — data first, no rule assumed.

Every exit tool so far compared rules I invented (stop / trail / scale-out /
target). That presupposes the answer. This one asks the prior question, from
data alone: for every trade the engine actually took, where was the BEST
possible exit, and is there anything observable at that moment?

For each round trip we reconstruct:
    actual   what the sleeve did
    oracle   the best exit available on the path it actually held through
             (and, separately, on the path out to end of session)

and then characterise the oracle:
    * WHEN  is it, relative to entry and to the actual exit
    * WHERE is it, as a fraction of the trade's maximum favourable excursion
    * how much was LEFT (oracle - actual), which is the size of the prize
    * is it CLUSTERED (a rule could find it) or SCATTERED (it cannot)

That last point decides everything downstream. If oracle exits cluster a fixed
time after entry, a clock works. If they cluster at a fraction of MFE, a
give-back rule works. If they cluster on a level derived from the setup, a
target works. If they are scattered, no rule of that shape can work and the
exit must be CONDITIONED on outside information -- which is the case worth
testing the whole roster's state against.

Sleeve positions come from claude_paper_fills, so this is what the engine really
did, not a simulation. Price paths come from claude_ticks_live where available
(both live sessions) and claude_bars_live otherwise.

    .venv/Scripts/python.exe tools/exit_oracle.py
    .venv/Scripts/python.exe tools/exit_oracle.py --from 2026-07-27
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB          # noqa: E402

PU = {"ES": 50.0, "NQ": 20.0}


def load(qdb: QuestDB, since: str):
    f = qdb.df("SELECT ts,symbol,sleeve,side,qty,price,tag FROM claude_paper_fills "
               f"WHERE ts >= '{since}T00:00:00.000000Z' ORDER BY sleeve, ts")
    f["t"] = pd.to_datetime(f["ts"])
    px = {}
    for s in f["symbol"].unique():
        tk = qdb.df("SELECT ts, price FROM claude_ticks_live "
                    f"WHERE symbol='{s}' AND ts >= '{since}T00:00:00.000000Z' ORDER BY ts")
        if len(tk) < 1000:                     # fall back to bars
            tk = qdb.df("SELECT ts, c AS price FROM claude_bars_live "
                        f"WHERE symbol='{s}' AND ts >= '{since}T00:00:00.000000Z' ORDER BY ts")
        tk["t"] = pd.to_datetime(tk["ts"])
        px[s] = tk.reset_index(drop=True)
    return f, px


def round_trips(f: pd.DataFrame):
    """Flat -> position -> flat, with the entry/exit that bracket it."""
    out = []
    for sl, g in f.groupby("sleeve"):
        pos, ent, entt, d = 0, None, None, 0
        for r in g.itertuples(index=False):
            q = int(r.side * r.qty)
            if pos == 0:
                pos, ent, entt, d = q, r.price, r.t, (1 if q > 0 else -1)
            else:
                out.append({"sleeve": sl, "symbol": g["symbol"].iloc[0], "dir": d,
                            "entry": ent, "entry_t": entt, "exit": r.price,
                            "exit_t": r.t, "exit_tag": r.tag})
                pos = 0
    return pd.DataFrame(out)


def analyse(rt: pd.DataFrame, px: dict, horizon_min: int):
    rows = []
    for r in rt.itertuples(index=False):
        p = px.get(r.symbol)
        if p is None or p.empty:
            continue
        end = r.exit_t + pd.Timedelta(minutes=horizon_min)
        held = p[(p.t >= r.entry_t) & (p.t <= r.exit_t)]
        fwd = p[(p.t >= r.entry_t) & (p.t <= end)]
        if len(held) < 2 or len(fwd) < 2:
            continue
        adv_held = r.dir * (held.price.to_numpy() - r.entry)
        adv_fwd = r.dir * (fwd.price.to_numpy() - r.entry)
        actual = r.dir * (r.exit - r.entry)
        i_held = int(np.argmax(adv_held))
        i_fwd = int(np.argmax(adv_fwd))
        best_held = float(adv_held[i_held])
        best_fwd = float(adv_fwd[i_fwd])
        t_held = (held.t.iloc[i_held] - r.entry_t).total_seconds() / 60
        t_fwd = (fwd.t.iloc[i_fwd] - r.entry_t).total_seconds() / 60
        hold_min = (r.exit_t - r.entry_t).total_seconds() / 60
        rows.append({
            "sleeve": r.sleeve, "symbol": r.symbol, "tag": r.exit_tag,
            "actual": actual, "hold_min": hold_min,
            "best_in_hold": best_held, "t_best_in_hold": t_held,
            "best_fwd": best_fwd, "t_best_fwd": t_fwd,
            "left_in_hold": best_held - actual,      # gave back before exiting
            "left_after": best_fwd - actual,         # total prize incl. after exit
            "frac_of_mfe": (actual / best_held) if best_held > 1e-9 else np.nan,
            "usd": actual * PU.get(r.symbol, 50.0),
        })
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="since", default="2026-07-27")
    ap.add_argument("--horizon", type=int, default=60, help="minutes past the exit")
    a = ap.parse_args()

    qdb = QuestDB(timeout=180.0)
    f, px = load(qdb, a.since)
    rt = round_trips(f)
    d = analyse(rt, px, a.horizon)
    if d.empty:
        raise SystemExit("no completed round trips with price data")

    print("\n" + "=" * 92)
    print(f"EXIT ORACLE — {len(d)} completed trades since {a.since}, "
          f"{d.sleeve.nunique()} sleeves")
    print("what the sleeve got vs what was THERE. no exit rule assumed.")
    print("=" * 92)

    tot_a = (d.actual * d.symbol.map(PU)).sum()
    tot_h = (d.best_in_hold * d.symbol.map(PU)).sum()
    tot_f = (d.best_fwd * d.symbol.map(PU)).sum()
    print(f"\n  actual P&L                        {tot_a:+12,.0f}$")
    print(f"  best exit WITHIN the hold         {tot_h:+12,.0f}$   "
          f"(left on the table {tot_h-tot_a:+,.0f}$)")
    print(f"  best exit within +{a.horizon}min of exit    {tot_f:+12,.0f}$   "
          f"(total prize {tot_f-tot_a:+,.0f}$)")

    print(f"\n{'':>4}{'metric':<34}{'p25':>9}{'median':>9}{'p75':>9}")
    for lbl, col in (("time to best exit (min)", "t_best_in_hold"),
                     ("actual hold (min)", "hold_min"),
                     ("captured / MFE", "frac_of_mfe"),
                     ("left in hold (pt)", "left_in_hold")):
        v = d[col].replace([np.inf, -np.inf], np.nan).dropna()
        if len(v):
            print(f"{'':>4}{lbl:<34}{v.quantile(.25):9.2f}{v.median():9.2f}"
                  f"{v.quantile(.75):9.2f}")

    print("\n[IS THE ORACLE FINDABLE?]  spread of the best-exit time, per sleeve")
    print(f"{'sleeve':>20}{'n':>4}{'med t*':>8}{'IQR t*':>8}{'med MFE':>9}"
          f"{'med left':>9}")
    for sl, g in d.groupby("sleeve"):
        t = g.t_best_in_hold
        print(f"{sl:>20}{len(g):4d}{t.median():8.1f}"
              f"{t.quantile(.75)-t.quantile(.25):8.1f}"
              f"{g.best_in_hold.median():9.2f}{g.left_in_hold.median():9.2f}")

    print("\n[BY EXIT TAG] what each exit REASON actually delivered")
    print(f"{'tag':>18}{'n':>4}{'med P&L':>10}{'med left':>10}{'med frac':>10}")
    for tg, g in d.groupby("tag"):
        print(f"{tg:>18}{len(g):4d}{g.actual.median():10.2f}"
              f"{g.left_in_hold.median():10.2f}"
              f"{g.frac_of_mfe.replace([np.inf,-np.inf],np.nan).median():10.2f}")

    print("\n  A TIGHT IQR on t* means a clock could find the exit.")
    print("  A tight `captured/MFE` means a give-back rule could.")
    print("  Both wide => no self-contained rule works and the exit has to be")
    print("  conditioned on something outside the trade.")
    print("=" * 92 + "\n")


if __name__ == "__main__":
    main()
