"""How much of claude_paper_fills is fiction?

Until 2026-07-29 a paper fill was stamped `clock.now()` (the OS wall clock) while
priced from `last_px` (the event stream). The two clocks disagreed by whatever the
processing lag was, so every row is a price welded to the wrong instant. Small lag
= small error; a feed replaying history = an entirely invented trade.

This audits the damage the only way that is falsifiable: a fill claims to have
traded at price P in minute M, so P must lie within minute M's high/low. Outside
it, the row did not happen as recorded.

    .venv/Scripts/python.exe tools/paper_audit.py [--days 30]

Reports per ET day: how many fills are impossible, and by how much. Use it to
decide which days of sleeve evidence can be trusted, and to confirm the days
after the fix stay clean.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine.adapters.questdb import QuestDB          # noqa: E402

TOL = 0.25          # one tick of slack for the bar aggregation itself
BAD = 2.0           # points outside the bar -> not explainable as aggregation


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--csv", default="", help="write the per-fill verdicts here")
    ap.add_argument("--source", default="",
                    help="audit a rebuilt record instead of the live table, e.g. "
                         ".cache/replay_fills_ES_live.csv — use this to PROVE a "
                         "regenerated record is clean before trusting it")
    a = ap.parse_args()
    qdb = QuestDB()

    if a.source:
        f = pd.read_csv(a.source)
        print(f"  auditing rebuilt record: {a.source} ({len(f)} fills)")
    else:
        f = qdb.df("SELECT ts, symbol, sleeve, side, qty, price, tag "
                   "FROM claude_paper_fills ORDER BY ts")
    b = qdb.df("SELECT symbol, ts, h, l FROM claude_bars_live")
    if f.empty:
        raise SystemExit("claude_paper_fills is empty")
    for d in (f, b):
        d["t"] = pd.to_datetime(d.ts, utc=True).dt.tz_convert("America/New_York")
        d["min"] = d.t.dt.floor("1min")
    # claude_bars_live.ts is the bar's CLOSE, not its open (verified by joining the
    # captured tick stream at offsets -2..+2: +1 puts 0.00% of both ES and NQ ticks
    # outside their bar, +0 leaves 33-41% out). So a fill has TWO admissible bars
    # and which one depends on what priced it:
    #   tick-priced  filled at time X inside a minute  -> the bar ending at
    #                floor(X)+1, i.e. the one still forming
    #   bar-priced   filled at a bar's CLOSE, stamped with that bar's own ts T
    #                -> the bar ending at T
    # A fill is plausible if its price sits in EITHER. Checking only floor+1 marked
    # 62 NQ bar-close fills "impossible" (worst 53pt) purely because price moved
    # away in the following minute; checking only floor mismarked the tick fills.
    # This audit has now been wrong twice, in opposite directions -- hence both.
    f["day"] = f.t.dt.strftime("%Y-%m-%d")
    bb = b[["symbol", "min", "h", "l"]]
    m = f.merge(bb.rename(columns={"h": "h1", "l": "l1"}),
                left_on=["symbol", "min"], right_on=["symbol", "min"], how="left")
    m["min2"] = m["min"] + pd.Timedelta(minutes=1)
    m = m.merge(bb.rename(columns={"min": "min2", "h": "h2", "l": "l2"}),
                on=["symbol", "min2"], how="left")
    o1 = (m.price - m.h1).clip(lower=0) + (m.l1 - m.price).clip(lower=0)
    o2 = (m.price - m.h2).clip(lower=0) + (m.l2 - m.price).clip(lower=0)
    # nearest admissible bar wins; a fill only fails if BOTH exclude it
    m["out"] = pd.concat([o1, o2], axis=1).min(axis=1, skipna=True)
    m["h"] = m.h2.fillna(m.h1)
    m["l"] = m.l2.fillna(m.l1)
    m.loc[m.out <= TOL, "out"] = 0.0
    m["impossible"] = m.out > BAD
    m["unverifiable"] = m.h.isna()          # no bar at all — capture was dead

    days = sorted(m.day.unique())[-a.days:]
    m = m[m.day.isin(days)]
    print(f"\n{'='*94}")
    print("PAPER RECORD AUDIT — a fill must sit inside the minute bar it claims")
    print(f"{'='*94}")
    print(f"{'day':>12}{'fills':>7}{'impossible':>12}{'no bar':>8}{'clean%':>8}"
          f"{'worst pt':>10}{'median bad':>12}  verdict")
    tot = {"n": 0, "imp": 0, "unv": 0}
    for d in days:
        g = m[m.day == d]
        imp, unv = int(g.impossible.sum()), int(g.unverifiable.sum())
        clean = len(g) - imp - unv
        worst = float(g.out.max()) if len(g) else 0.0
        medbad = float(g.loc[g.impossible, "out"].median()) if imp else 0.0
        pct = 100.0 * clean / max(len(g), 1)
        verdict = ("USABLE" if pct >= 95 else
                   "SUSPECT" if pct >= 60 else "UNUSABLE")
        print(f"{d:>12}{len(g):>7}{imp:>12}{unv:>8}{pct:>7.0f}%"
              f"{worst:>10.2f}{medbad:>12.2f}  {verdict}")
        tot["n"] += len(g)
        tot["imp"] += imp
        tot["unv"] += unv
    ok = tot["n"] - tot["imp"] - tot["unv"]
    print(f"\n{'TOTAL':>12}{tot['n']:>7}{tot['imp']:>12}{tot['unv']:>8}"
          f"{100.0*ok/max(tot['n'],1):>7.0f}%")
    print(f"\n  {tot['imp']} fills ({100.0*tot['imp']/max(tot['n'],1):.0f}%) record a "
          f"price the market never traded in that minute.")
    print(f"  {tot['unv']} more ({100.0*tot['unv']/max(tot['n'],1):.0f}%) cannot be "
          f"checked at all — no bar was captured for that minute.")
    print("\n  Per-sleeve exposure (impossible fills), worst first:")
    s = (m.groupby("sleeve")
          .agg(fills=("price", "size"), impossible=("impossible", "sum"),
               worst=("out", "max"))
          .sort_values("impossible", ascending=False))
    s["pct"] = 100.0 * s.impossible / s.fills
    for r in s[s.impossible > 0].head(20).itertuples():
        print(f"    {r.Index:<26} {r.impossible:>4}/{r.fills:<4} "
              f"({r.pct:>3.0f}%)  worst {r.worst:>8.2f} pt")
    if a.csv:
        m.to_csv(a.csv, index=False)
        print(f"\n  wrote {a.csv}")
    print(f"{'='*94}\n")


if __name__ == "__main__":
    main()
