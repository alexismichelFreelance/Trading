"""Exits that reference the MARKET, not the clock and not your own P&L.

Everything tested so far failed on the same principle. `time Nm` has no
information content -- it works on days whose rhythm happens to match it and
fails otherwise, which is precisely why `time 30m` won on 29 trades and lost
out-of-sample on 15 sessions. `giveback f` is self-referential: it looks only at
the trade's own excursion and GUARANTEES surrendering f of every winner. Neither
looks at the market.

(On the Fibonacci parallel: retracement levels in the literature are
ANTICIPATED LEVELS where price may turn -- structure you rest an order at. "Exit
when my P&L has retraced 50%" is a different and weaker construct that happens
to share a number.)

A real exit references structure price is ARRIVING AT. Every level here already
exists in the engine and none is a constant invented for this test:

    D/W/M floor pivots   engine.features.pivots.MultiPivots
    session VWAP         volume-weighted, from the session's own bars
    overnight H/L        the 00:00-09:30 ET range
    prior-day H/L        the previous session's extremes
    opening range H/L    09:30-10:00 ET

For each trade we take the FIRST touch of the next such level in the trade's
favour and compare it against what the sleeve actually did, and against the
oracle (the best exit that existed on that path).

The comparison that matters is not "which level wins" but whether ANY
structural exit beats the sleeve's own -- if structure is where price turns,
exiting there should beat exiting on a clock.

    .venv/Scripts/python.exe tools/exit_structural.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB              # noqa: E402
from engine.features.pivots import daily_pivots          # noqa: E402
from tools.exit_oracle import load, round_trips          # noqa: E402

PU = {"ES": 50.0, "NQ": 20.0}
RTH_OPEN, RTH_OR_END = 9 * 60 + 30, 10 * 60


def session_levels(qdb: QuestDB, symbol: str, since: str) -> dict:
    """Structural levels per session, built from 24h 1m bars."""
    b = qdb.df("SELECT ts,o,h,l,c,vol FROM claude_bars_live "
               f"WHERE symbol='{symbol}' AND ts >= '{since}T00:00:00.000000Z' "
               "ORDER BY ts")
    if b.empty:
        return {}
    b["t"] = pd.to_datetime(b["ts"], utc=True)
    et = b["t"].dt.tz_convert("America/New_York")
    b["day"] = et.dt.strftime("%Y-%m-%d")
    b["min"] = et.dt.hour * 60 + et.dt.minute
    out, prev = {}, None
    for day, g in b.groupby("day", sort=True):
        on = g[g["min"] < RTH_OPEN]                 # overnight
        rth = g[g["min"] >= RTH_OPEN]
        orr = rth[rth["min"] < RTH_OR_END]          # opening range
        lv = {}
        if prev is not None:
            lv["pdh"], lv["pdl"] = float(prev.h.max()), float(prev.l.min())
            p = daily_pivots(float(prev.h.max()), float(prev.l.min()),
                             float(prev.c.iloc[-1]))
            for k in ("PP", "R1", "R2", "R3", "S1", "S2", "S3"):
                lv[f"piv_{k}"] = float(p[k])
        if len(on):
            lv["on_hi"], lv["on_lo"] = float(on.h.max()), float(on.l.min())
        if len(orr):
            lv["or_hi"], lv["or_lo"] = float(orr.h.max()), float(orr.l.min())
        if len(rth):
            tp = (rth.h + rth.l + rth.c) / 3.0
            v = rth.vol.replace(0, 1)
            lv["vwap"] = float((tp * v).sum() / v.sum())
        out[day] = lv
        prev = g
    return out


def first_touch(path: pd.DataFrame, d: int, entry: float, level: float):
    """P&L of exiting at the first touch of `level`, if it is in our favour."""
    if level is None or not np.isfinite(level):
        return None
    if (level - entry) * d <= 0:                    # level is behind us
        return None
    p = path.price.to_numpy(float)
    hit = np.flatnonzero((p - level) * d >= 0)
    if not len(hit):
        return None
    return (level - entry) * d


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="since", default="2026-07-27")
    ap.add_argument("--min-hold", type=float, default=2.0)
    a = ap.parse_args()

    qdb = QuestDB(timeout=180.0)
    f, px = load(qdb, a.since)
    rt = round_trips(f)
    lv = {s: session_levels(qdb, s, a.since) for s in f["symbol"].unique()}

    names = ["vwap", "on_hi", "on_lo", "or_hi", "or_lo", "pdh", "pdl",
             "piv_PP", "piv_R1", "piv_R2", "piv_R3", "piv_S1", "piv_S2", "piv_S3"]
    rows = []
    for r in rt.itertuples(index=False):
        p = px.get(r.symbol)
        if p is None or p.empty:
            continue
        seg = p[(p.t >= r.entry_t) & (p.t <= r.exit_t)]
        if len(seg) < 20 or (r.exit_t - r.entry_t).total_seconds() / 60 < a.min_hold:
            continue
        day = r.entry_t.tz_convert("America/New_York").strftime("%Y-%m-%d")
        L = lv.get(r.symbol, {}).get(day, {})
        adv = r.dir * (seg.price.to_numpy(float) - r.entry)
        row = {"sym": r.symbol, "actual": r.dir * (r.exit - r.entry),
               "ORACLE": float(adv.max())}
        for n in names:
            row[n] = first_touch(seg, r.dir, r.entry, L.get(n))
        # NEAREST structural level in our favour, whichever type it is
        cands = [(L[n], n) for n in names if n in L and (L[n] - r.entry) * r.dir > 0]
        row["nearest_level"] = (first_touch(seg, r.dir, r.entry,
                                            min(cands, key=lambda x: abs(x[0]-r.entry))[0])
                                if cands else None)
        rows.append(row)
    if not rows:
        raise SystemExit("no usable trades")
    D = pd.DataFrame(rows)
    mult = D["sym"].map(PU).to_numpy()

    print("\n" + "=" * 82)
    print(f"STRUCTURAL EXITS — {len(D)} trades since {a.since}")
    print("exit at the first touch of a level the MARKET defines.")
    print("un-touched levels fall back to the sleeve's own exit, so a rule is")
    print("never credited for a trade where its level never traded.")
    print("=" * 82)
    base = float((D["actual"] * mult).sum())
    orac = float((D["ORACLE"] * mult).sum())
    print(f"\n  actual  {base:+11,.0f}$     ORACLE {orac:+11,.0f}$")
    print(f"\n{'level':>16}{'n hit':>7}{'total $':>12}{'vs actual':>12}"
          f"{'% oracle':>10}{'win%':>7}")
    res = []
    for n in names + ["nearest_level"]:
        v = D[n]
        used = v.notna()
        if used.sum() < 3:
            continue
        pnl = np.where(used, v.fillna(0.0), D["actual"])
        tot = float((pnl * mult).sum())
        res.append((n, int(used.sum()), tot, 100 * (pnl > 0).mean()))
    for n, k, tot, win in sorted(res, key=lambda x: -x[2]):
        star = "  <<<" if tot > base else ""
        print(f"{n:>16}{k:7d}{tot:+12,.0f}{tot-base:+12,.0f}"
              f"{100*tot/orac if orac else 0:9.1f}%{win:6.0f}%{star}")
    print("=" * 82 + "\n")


if __name__ == "__main__":
    main()
