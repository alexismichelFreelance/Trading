"""Would gating the existing sleeves on the gamma pocket have helped?

Per-bar, short pockets amplify (VR30 1.13 ES / 1.25 NQ) and long pockets pin
(0.83 / 0.58), strongest deep and absent near the edge
(strategy_lab/gamma_pocket_behaviour.py). That is a statement about price, not
about P&L, and the two do not follow from each other.

So before building a gate: take the trades the sleeves ALREADY made in the
pinned replay, look up which pocket price was in AT ENTRY, and split each
sleeve's result by it. No new code in the engine, no new replay -- if the split
shows nothing there is nothing to gate on.

Trend sleeves should earn in SHORT pockets and mean-reversion sleeves in LONG
ones. Near an edge both regimes revert, so both should do worse there.

Reads .cache/replay_fills_ES_live.csv, which is the corrected-fill record.
"""
from __future__ import annotations

import collections
import csv
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB              # noqa: E402
from engine.features.gamma_profile import gamma_profile  # noqa: E402

ET = dt.timezone(dt.timedelta(hours=-4))
PV = 50.0
TREND = ("trendjoin", "onbreak", "opendrive", "vwapbreak", "ignition")
REVERT = ("zones", "dipbuy", "ibs", "rsi2", "pivot", "sweepfade")


def kind(sleeve: str) -> str:
    s = sleeve.split(":")[-1]
    if any(s.startswith(t) for t in TREND):
        return "trend"
    if any(s.startswith(t) for t in REVERT):
        return "revert"
    return "other"


def main() -> None:
    q = QuestDB(timeout=240)
    gx = q.df("SELECT ts,underlying,strike,call_gex,put_gex,spot "
              "FROM claude_gex_strikes WHERE underlying='SPX' ORDER BY ts")
    gx["day"] = gx["ts"].dt.strftime("%Y-%m-%d")
    curves = {}
    for d, g in gx.groupby("day"):
        agg: dict = {}
        for r in g.itertuples():
            c, p = agg.get(r.strike, (0.0, 0.0))
            agg[r.strike] = (c + float(r.call_gex), p + float(r.put_gex))
        curves[d] = (agg, float(g["spot"].iloc[0]))

    b = q.df("SELECT ts,c FROM claude_bars_live WHERE symbol='ES' ORDER BY ts")
    et = pd.to_datetime(b["ts"], utc=True).dt.tz_convert("America/New_York")
    m = et.dt.hour * 60 + et.dt.minute
    b = b[(m >= 570) & (m < 960)].copy()
    b["day"] = et[(m >= 570) & (m < 960)].dt.strftime("%Y-%m-%d")
    closes = b.groupby("day")["c"].last()
    cdays = sorted(curves)
    basis = {}
    for d in cdays:
        prev = [x for x in closes.index if x < d]
        if prev:
            basis[d] = float(closes[prev[-1]]) - curves[d][1]

    # cache the curve arrays per source session
    prep = {}
    for d in cdays:
        if d not in basis:
            continue
        pr = gamma_profile(curves[d][0], curves[d][1])
        if pr is None:
            continue
        prep[d] = (np.array([k for k, _, _ in pr["curve"]]),
                   np.array([c for _, _, c in pr["curve"]]),
                   np.array([f + basis[d] for f in pr["flips"]]), basis[d])

    rows = sorted(csv.DictReader(
        open(ROOT / ".cache" / "replay_fills_ES_live.csv", encoding="utf-8-sig")),
        key=lambda r: float(r["ts"]))

    # flat-to-flat trades per sleeve, tagged with the pocket at ENTRY
    books: dict = collections.defaultdict(lambda: [0, 0.0, None])
    out = []
    for r in rows:
        s = r["sleeve"]
        qty = int(float(r["side"])) * int(float(r["qty"]))
        px = float(r["price"])
        when = dt.datetime.fromtimestamp(int(float(r["ts"])) / 1e9, ET)
        day = when.strftime("%Y-%m-%d")
        pos, avg, ent = books[s]
        if pos == 0:
            src = [x for x in prep if x < day]
            state = None
            if src:
                ks, cums, flips, bs = prep[src[-1]]
                cum = float(np.interp(px - bs, ks, cums))
                edge = float(np.min(np.abs(px - flips))) if len(flips) else np.inf
                state = ("LONG" if cum > 0 else "SHORT", edge)
            books[s] = [qty, px, state]
            continue
        if (qty > 0) != (pos > 0):
            n = min(abs(qty), abs(pos))
            pl = (px - avg) * (1 if pos > 0 else -1) * n * PV
            if ent:
                out.append(dict(sleeve=s, kind=kind(s), regime=ent[0],
                                edge=ent[1], pnl=pl))
            newpos = pos + qty
            books[s] = [newpos, px if newpos and (newpos > 0) != (pos > 0) else avg,
                        ent if newpos else None]
        else:
            books[s] = [pos + qty,
                        (avg * abs(pos) + px * abs(qty)) / (abs(pos) + abs(qty)), ent]

    d = pd.DataFrame([r for r in out if r["regime"]])
    if d.empty:
        print("no trades could be matched to a pocket")
        return
    med = d[np.isfinite(d["edge"])]["edge"].median()
    d["depth"] = np.where(d["edge"] > med, "deep", "near edge")

    print(f"\nES trades matched to a pocket at entry: {len(d)}   "
          f"(edge split at {med:.1f} pts)\n")
    print(f"  {'sleeve kind':12} {'regime':7} {'trips':>6} {'total $':>10} {'mean':>8} {'win%':>6}")
    for (k, reg), g in d.groupby(["kind", "regime"]):
        print(f"  {k:12} {reg:7} {len(g):>6} {g['pnl'].sum():>10,.0f} "
              f"{g['pnl'].mean():>8,.0f} {100 * (g['pnl'] > 0).mean():>5.0f}%")
    print(f"\n  {'sleeve kind':12} {'regime':7} {'depth':10} {'trips':>6} {'total $':>10}")
    for (k, reg, dep), g in d.groupby(["kind", "regime", "depth"]):
        print(f"  {k:12} {reg:7} {dep:10} {len(g):>6} {g['pnl'].sum():>10,.0f}")

    print("\n  PER SLEEVE (>=4 trips in a regime)")
    for (s, reg), g in d.groupby(["sleeve", "regime"]):
        if len(g) >= 4:
            print(f"    {s:26} {reg:7} {len(g):>4} trips {g['pnl'].sum():>9,.0f}"
                  f"  mean {g['pnl'].mean():>7,.0f}")


if __name__ == "__main__":
    main()
