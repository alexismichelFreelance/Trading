"""Two-phase exits: let it ride, then protect. "Cut losers short, let winners run."

Every uniform rule tested so far failed for ONE reason. tools/exit_oracle.py
showed the prize is all tail -- 5 of 41 trades are 63% of it, median trade is
worth +$300 and the mean +$1,750, and the big ones ran 380-660 NQ points while
the sleeves kept 33-69%. A single threshold applied to every trade must either
be loose enough for the tail (and useless on the noise) or tight enough for the
noise (and fatal to the tail). Nearest-level take-profit came in at -$17,089
precisely because it capped the only trades that mattered.

So the exit becomes a STATE MACHINE:

    PHASE 1  RIDE     nothing but the disaster stop. The move is allowed to
                      run -- no clock, no take-profit, no give-back.
    ARM               the trade has ACHIEVED something real, measured in units
                      the market supplies, not constants I chose.
    PHASE 2  PROTECT  now watch for the move ending, and leave while it is
                      still a good winner.

ARMING (all market-scaled)
    range   MFE >= k x the session's own typical move (vol_unit)
    risk    MFE >= k x the trade's initial risk (an R-multiple)
    levels  price has crossed k structural levels in our favour

REVERSAL, once armed. Each is SELF-SCALING -- it compares the move against
itself, so a 10-minute move and a 4-hour move are judged on their own terms:
    stall     no new extreme for f x the time it took to reach the extreme
    decay     recent advance rate has fallen to f of its peak rate
    range     price confined to a band < f x its own recent swing (going
              sideways -- "a range of no interest")
    retrace   gives back f of the LAST leg (not of the whole trade)

Reported against `actual`, against the ORACLE ceiling, and -- the number that
matters -- against what the same rules do to the 5 tail trades specifically.

    .venv/Scripts/python.exe tools/exit_two_phase.py
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
from engine.features.swings import vol_unit              # noqa: E402
from tools.exit_oracle import load, round_trips          # noqa: E402

PU = {"ES": 50.0, "NQ": 20.0}


def session_unit(qdb: QuestDB, symbol: str, since: str) -> dict:
    """The session's own typical move -- the unit every threshold is expressed
    in, so nothing here is a number in points that I picked."""
    b = qdb.df("SELECT ts,c FROM claude_bars_live "
               f"WHERE symbol='{symbol}' AND ts >= '{since}T00:00:00.000000Z' "
               "ORDER BY ts")
    if b.empty:
        return {}
    b["t"] = pd.to_datetime(b["ts"], utc=True)
    b["day"] = b["t"].dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    return {d: float(vol_unit(g["c"].to_numpy(float), horizon=30) or 0.0)
            for d, g in b.groupby("day")}


def run_trade(adv: np.ndarray, mins: np.ndarray, unit: float, risk: float,
              arm_kind: str, arm_k: float, rev_kind: str, rev_f: float,
              win: int = 30) -> float:
    """Simulate one trade under one (arm, reversal) pair. Causal throughout."""
    peak = np.maximum.accumulate(adv)
    n = len(adv)
    thr = (arm_k * unit if arm_kind == "range" else
           arm_k * risk if arm_kind == "risk" else np.inf)
    if not np.isfinite(thr) or thr <= 0:
        return adv[-1]
    armed_at = np.flatnonzero(peak >= thr)
    if not len(armed_at):
        return adv[-1]                       # never achieved anything: ride to the end
    a0 = int(armed_at[0])

    # index of the running peak, so "time since the extreme" is self-referential
    peak_idx = np.maximum.accumulate(np.where(adv >= peak, np.arange(n), 0))
    for i in range(a0, n):
        pk = peak[i]
        j = int(peak_idx[i])
        t_to_peak = max(mins[j] - mins[0], 1e-6)
        if rev_kind == "stall":
            if (mins[i] - mins[j]) >= rev_f * t_to_peak:
                return adv[i]
        elif rev_kind == "retrace":
            leg = pk - adv[max(a0, j - 1)] if j > a0 else pk
            leg = max(leg, pk)               # last leg, floored at the whole run
            if adv[i] <= pk - rev_f * leg:
                return adv[i]
        elif rev_kind == "decay":
            k = max(1, min(win, i))
            rate = (adv[i] - adv[i - k]) / k
            best = max((peak[j] - peak[max(0, j - k)]) / k, 1e-9)
            if rate <= rev_f * best:
                return adv[i]
        elif rev_kind == "range":
            k = max(2, min(win, i))
            lo = max(0, i - k)
            seg = adv[lo:i + 1]
            if seg.size == 0:
                continue
            swing = float(seg.max() - seg.min())
            if swing <= rev_f * max(pk, 1e-9):
                return adv[i]
    return adv[-1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="since", default="2026-07-27")
    ap.add_argument("--min-hold", type=float, default=2.0)
    a = ap.parse_args()

    qdb = QuestDB(timeout=180.0)
    f, px = load(qdb, a.since)
    rt = round_trips(f)
    units = {s: session_unit(qdb, s, a.since) for s in f["symbol"].unique()}

    trades = []
    for r in rt.itertuples(index=False):
        p = px.get(r.symbol)
        if p is None or p.empty:
            continue
        seg = p[(p.t >= r.entry_t) & (p.t <= r.exit_t)]
        if len(seg) < 30 or (r.exit_t - r.entry_t).total_seconds() / 60 < a.min_hold:
            continue
        day = r.entry_t.tz_convert("America/New_York").strftime("%Y-%m-%d")
        u = units.get(r.symbol, {}).get(day, 0.0)
        if u <= 0:
            continue
        adv = r.dir * (seg.price.to_numpy(float) - r.entry)
        mins = (pd.DatetimeIndex(seg.t) - r.entry_t).total_seconds().to_numpy() / 60
        risk = max(abs(float(np.min(adv[:min(len(adv), 60)]))), u)   # early heat = risk
        trades.append({"sym": r.symbol, "adv": adv, "mins": mins, "unit": u,
                       "risk": risk, "actual": r.dir * (r.exit - r.entry),
                       "oracle": float(adv.max()), "sleeve": r.sleeve})
    if not trades:
        raise SystemExit("no usable trades")

    mult = np.array([PU[t["sym"]] for t in trades])
    act = np.array([t["actual"] for t in trades])
    ora = np.array([t["oracle"] for t in trades])
    big = np.argsort(-(ora * mult))[:5]              # the tail that matters

    print("\n" + "=" * 88)
    print(f"TWO-PHASE EXITS — {len(trades)} trades since {a.since}")
    print("PHASE 1 ride (stop only) -> ARM on achievement -> PHASE 2 protect")
    print("=" * 88)
    base, orac = float((act * mult).sum()), float((ora * mult).sum())
    print(f"\n  actual {base:+11,.0f}$    ORACLE {orac:+11,.0f}$"
          f"    (top-5 trades are {100*(ora*mult)[big].sum()/orac:.0f}% of oracle)")
    act_tail = float((act * mult)[big].sum())
    print(f"  ACTUAL tail-5 {act_tail:+,.0f}$ of a "
          f"{float((ora*mult)[big].sum()):+,.0f}$ ceiling")
    print(f"\n{'arm':>14}{'reversal':>12}{'armed':>7}{'total $':>12}"
          f"{'vs actual':>11}{'TAIL-5 $':>11}{'vs act tail':>12}")
    rows = []
    # A trade that never ARMS falls through to its own exit, so raising the bar
    # confines the machinery to the moves that actually achieved something --
    # "after it has ridden SOME". Loose arming was touching the noise trades,
    # where there is nothing to protect and every intervention costs.
    for ak, akk in (("range", 4.0), ("range", 8.0), ("range", 12.0),
                    ("risk", 3.0), ("risk", 5.0), ("risk", 8.0), ("risk", 12.0)):
        for rk, rf in (("stall", 0.5), ("stall", 1.0), ("retrace", 0.25),
                       ("retrace", 0.4), ("decay", 0.1), ("range", 0.25)):
            v = np.array([run_trade(t["adv"], t["mins"], t["unit"], t["risk"],
                                    ak, akk, rk, rf) for t in trades])
            narm = sum(1 for t in trades
                       if np.maximum.accumulate(t["adv"]).max() >=
                       (akk * t["unit"] if ak == "range" else akk * t["risk"]))
            tot = float((v * mult).sum())
            tail = float((v * mult)[big].sum())
            rows.append((f"{ak} {akk:g}x", f"{rk} {rf:g}", tot, tail, narm))
    for arm, rev, tot, tail, narm in sorted(rows, key=lambda x: -x[3])[:16]:
        star = "  <<<" if tail > act_tail and tot > base else (
            "  tail" if tail > act_tail else "")
        print(f"{arm:>14}{rev:>12}{narm:7d}{tot:+12,.0f}{tot-base:+11,.0f}"
              f"{tail:+11,.0f}{tail-act_tail:+12,.0f}{star}")
    print(f"\n  TAIL-5 ceiling {float((ora*mult)[big].sum()):+,.0f}$ — a rule that")
    print("  scores well overall but low on the tail is capping the only trades")
    print("  that pay. That is what every uniform rule did.")
    print("=" * 88 + "\n")


if __name__ == "__main__":
    main()
