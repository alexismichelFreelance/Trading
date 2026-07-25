"""Calibrate FlowFollowingStrategy's adaptive threshold.

`flow` and `flow_gex` fired ZERO orders in 34 captured sessions and `flow_fixed`
fired twice (tools/sleeve_audit.py). The adaptive gate is
    th_t = rolling_mean(|adelta|) + adapt_k * rolling_std(|adelta|)
over a trailing vol_win. |adelta| is heavy-tailed, so adapt_k=4.0 is effectively
"never" -- the same failure mode as PivotStrategy's unreachable ER_MIN=0.30.

Rather than guess a replacement, this measures the actual fire rate of the real
strategy object at each candidate k over the captured live stream, so the knob
is chosen for a target trade frequency and then frozen.

    .venv/Scripts/python.exe tools/flow_calib.py
    .venv/Scripts/python.exe tools/flow_calib.py --symbol NQ --ks 1.0,1.5,2.0

Judge on ENTRIES/session, not raw orders: flow scales in and out, so one thesis
can be several orders. A sane intraday flow sleeve wants a handful of entries
per session -- single digits, not hundreds (which is churn, not signal).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB                  # noqa: E402
from engine.core.timeutil import et_session_date             # noqa: E402
from engine.strategies.flow import FlowFollowingStrategy     # noqa: E402
from tools.sleeve_audit import build_events, drive_all       # noqa: E402


def run(symbol: str, ks: list[float], maxp: int) -> None:
    qdb = QuestDB(timeout=180.0)
    print(f"loading {symbol} capture ...", flush=True)
    events = build_events(qdb, symbol)
    if not events:
        raise SystemExit(f"no captured events for {symbol}")
    days = sorted({et_session_date(ts) for ts, _, _ in events})

    strats = {f"k={k:g}": FlowFollowingStrategy(symbol, maxp=maxp, adaptive=True,
                                                adapt_k=k) for k in ks}
    acc = drive_all(strats, events)

    print("\n" + "=" * 72)
    print(f"FLOW adaptive-threshold calibration — {symbol}, {len(days)} sessions "
          f"({days[0]}..{days[-1]})")
    print("th = rolling_mean(|adelta|) + k * rolling_std(|adelta|)")
    print("=" * 72)
    print(f"\n{'k':>6} {'orders':>8} {'entries':>8} {'days':>5} {'fire%':>7} "
          f"{'entries/day':>12}  verdict")
    for lb, a in acc.items():
        k = lb.split("=")[1]
        nd = len(a["days"])
        pct = 100.0 * nd / len(days)
        epd = a["entries"] / nd if nd else 0.0
        verdict = ("DEAD" if a["orders"] == 0 else
                   "too rare" if pct < 10 else
                   "CHURN" if epd > 25 else "usable")
        print(f"{k:>6} {a['orders']:8d} {a['entries']:8d} {nd:5d} {pct:6.1f}% "
              f"{epd:12.1f}  {verdict}")
    print("\npick the k whose entries/day matches the intended trade rate, then")
    print("FREEZE it in tools/run_live.py and re-run tools/sleeve_audit.py.\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ES")
    ap.add_argument("--ks", default="1.0,1.5,2.0,2.5,3.0,4.0")
    ap.add_argument("--maxp", type=int, default=5)
    a = ap.parse_args()
    run(a.symbol, [float(x) for x in a.ks.split(",")], a.maxp)


if __name__ == "__main__":
    main()
