"""Functional scorecard: what each sleeve ACTUALLY did, from the clean record.

Not a P&L table. The question is whether a sleeve is a strategy at all:

  * did it fire?            a sleeve with 2 trades in 18 sessions is not being
                            evaluated, it is burning CPU and cluttering the roster.
  * how does it EXIT?       the exit-reason mix is the tell. A sleeve whose exits
                            are mostly `moc` / `timeout` / `session-flat` has no
                            exit thesis -- it holds until something external ends
                            the trade. That is not an exit, it is an expiry.
  * how long does it hold?  a "day trade" holding 300+ minutes to the close is a
                            different animal from what its docstring claims.
  * does the exit keep any of the move, or hand it back?

Runs on .cache/replay_fills_{SYM}_live.csv -- the records rebuilt from captured
tape through the fixed engine, which tools/paper_audit.py verifies as 100% clean.
The live claude_paper_fills table before 2026-07-30 is NOT usable for this.

    .venv/Scripts/python.exe tools/sleeve_review.py --symbol ES
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PU = {"ES": 50.0, "NQ": 20.0}
# exits that are NOT a decision: the trade ended because the clock ran out
EXPIRY = ("moc", "session-flat", "timeout", "window-flat", "eod", "safety-flat")


def round_trips(f: pd.DataFrame) -> pd.DataFrame:
    out = []
    for sl, g in f.groupby("sleeve"):
        pos, avg, ent, ent_tag = 0, 0.0, None, ""
        for r in g.sort_values("t").itertuples(index=False):
            s, q, p = int(r.side), int(r.qty), float(r.price)
            if pos == 0 or (pos > 0) == (s > 0):
                if pos == 0:
                    ent, ent_tag = r.t, r.tag
                avg = (avg * abs(pos) + p * q) / (abs(pos) + q)
                pos += s * q
            else:
                n = min(q, abs(pos))
                d = 1 if pos > 0 else -1
                out.append({"sleeve": sl, "dir": d, "entry": avg, "exit": p,
                            "pts": (p - avg) * d, "qty": n, "in": ent, "out": r.t,
                            "mins": (r.t - ent).total_seconds() / 60,
                            "entry_tag": ent_tag, "exit_tag": r.tag})
                pos += s * q
                if pos != 0 and (pos > 0) != (d > 0):
                    avg, ent, ent_tag = p, r.t, r.tag
    return pd.DataFrame(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ES")
    a = ap.parse_args()
    src = ROOT / ".cache" / f"replay_fills_{a.symbol}_live.csv"
    f = pd.read_csv(src)
    f["t"] = pd.to_datetime(f.ts, utc=True).dt.tz_convert("America/New_York")
    f["day"] = f.t.dt.strftime("%Y-%m-%d")
    days = f.day.nunique()
    T = round_trips(f)
    if T.empty:
        raise SystemExit("no round trips")
    T["usd"] = T.pts * T.qty * PU[a.symbol]
    T["expiry"] = T.exit_tag.str.lower().apply(
        lambda s: any(e in s for e in EXPIRY))

    print(f"\n{'='*104}")
    print(f"SLEEVE FUNCTIONAL REVIEW — {a.symbol}, {days} sessions, "
          f"{len(T)} round trips (clean rebuilt record)")
    print(f"{'='*104}")
    print(f"{'sleeve':<26}{'trips':>6}{'/day':>6}{'win%':>6}{'medHold':>8}"
          f"{'maxHold':>8}{'EXPIRY%':>8}{'total$':>10}  top exit reasons")
    rows = []
    for sl, g in T.groupby("sleeve"):
        exits = g.exit_tag.value_counts()
        top = ", ".join(f"{k}:{v}" for k, v in exits.head(3).items())
        rows.append({
            "sleeve": sl, "n": len(g), "per_day": len(g) / days,
            "win": 100.0 * (g.pts > 0).mean(), "med": g.mins.median(),
            "mx": g.mins.max(), "exp": 100.0 * g.expiry.mean(),
            "usd": g.usd.sum(), "top": top})
    R = pd.DataFrame(rows).sort_values("usd", ascending=False)
    for r in R.itertuples(index=False):
        print(f"{r.sleeve:<26}{r.n:>6}{r.per_day:>6.1f}{r.win:>6.0f}"
              f"{r.med:>8.0f}{r.mx:>8.0f}{r.exp:>7.0f}%{r.usd:>+10,.0f}  {r.top}")

    print(f"\n{'-'*104}")
    print("VERDICT INPUTS")
    print(f"{'-'*104}")
    dead = R[R.per_day < 0.15]
    print(f"\n  BARELY FIRES (<1 trade per 7 sessions) -- not being evaluated, "
          f"just costing CPU:")
    for r in dead.itertuples(index=False):
        print(f"    {r.sleeve:<26} {r.n} trips in {days} sessions")
    noexit = R[R.exp >= 50]
    print(f"\n  NO EXIT THESIS (>=50% of trades ended by the clock, not a decision):")
    for r in noexit.sort_values("exp", ascending=False).itertuples(index=False):
        print(f"    {r.sleeve:<26} {r.exp:>3.0f}% expiry, median hold {r.med:>4.0f}m"
              f", {r.usd:>+9,.0f}$")
    hold = R[R.med >= 120]
    print(f"\n  HOLDS FOR HOURS (median >= 2h) -- check this is intended:")
    for r in hold.sort_values("med", ascending=False).itertuples(index=False):
        print(f"    {r.sleeve:<26} median {r.med:>4.0f}m  max {r.mx:>4.0f}m")
    print(f"\n{'='*104}\n")


if __name__ == "__main__":
    main()
