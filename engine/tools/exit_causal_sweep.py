"""Causal-unit sweep: what IS the session's typical move when the session is
five minutes old?

A flaw in tools/exit_two_phase.py, found by asking exactly that. It computed the
arming unit as vol_unit over the WHOLE session's bars, grouped by day -- i.e.
including bars AFTER the trade. That is lookahead, and it is favourable
lookahead: on a day that turned out volatile the unit is larger, so 12x arms
LATER, so the rule rides longer -- precisely on the big-move days that carried
the result. The live TwoPhaseExit instead uses a trailing window, which is
causal but is NOT what was measured. The research number and the sleeve were
never the same object.

This re-measures with a strictly CAUSAL unit: the median |move over `win`
samples| taken only over prices that existed BEFORE the trade was entered,
which is exactly what TwoPhaseExit.start() sees live. And because the unit is
now honest, the multiplier can be swept to answer the other question -- whether
12x was ever the right number, or just the one that fitted ES.

    .venv/Scripts/python.exe tools/exit_causal_sweep.py --symbol ESH5
    .venv/Scripts/python.exe tools/exit_causal_sweep.py --symbol ESM5
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.exit_oracle import round_trips            # noqa: E402
from tools.exit_two_phase import run_trade           # noqa: E402

PU = {"ESM5": 50.0, "ESH5": 50.0}
ARMS = [2, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256]
REVS = (("decay", 0.1), ("range", 0.25), ("retrace", 0.25), ("retrace", 0.4))


def causal_unit(prices: np.ndarray, win: int = 30) -> float:
    """Median absolute move over `win` samples, using ONLY the prices supplied.
    Callers pass the pre-entry slice, so nothing after the entry can leak in."""
    n = len(prices)
    if n <= win:
        return 0.0
    d = np.abs(prices[win:] - prices[:-win])
    d = d[np.isfinite(d)]
    return float(np.median(d)) if d.size else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ESH5")
    ap.add_argument("--min-hold", type=float, default=2.0)
    ap.add_argument("--vol-win", type=int, default=600,
                    help="trailing window for the unit (as live)")
    ap.add_argument("--grid", choices=("tick", "minute"), default="minute",
                    help="'minute' matches TwoPhaseExit's one-minute grid; "
                         "'tick' reproduces the old per-print behaviour")
    a = ap.parse_args()

    fills = pd.read_csv(ROOT / ".cache" / f"replay_fills_{a.symbol}.csv")
    fills["t"] = pd.to_datetime(fills["ts"], utc=True)
    fills["symbol"] = a.symbol
    px = []
    for f in sorted((ROOT / ".cache" / "replay").glob(f"{a.symbol}_*.npz")):
        z = np.load(f)
        m = z["kind"] == 1
        px.append(pd.DataFrame({"ts": z["ts"][m], "price": z["a"][m]}))
    P = pd.concat(px, ignore_index=True).sort_values("ts").reset_index(drop=True)
    P["t"] = pd.to_datetime(P["ts"], utc=True)
    allpx = P["price"].to_numpy(float)
    allts = P["ts"].to_numpy()              # int64 ns; avoids tz-aware comparison
    if a.grid == "minute":
        # Collapse to one close per minute, exactly as TwoPhaseExit.note_price
        # now does, so the ruler measured here IS the ruler the sleeve runs.
        mi = allts // 60_000_000_000
        keep = np.r_[mi[1:] != mi[:-1], True]          # last print of each minute
        upx, uts = allpx[keep], allts[keep]
    else:
        upx, uts = allpx, allts

    rt = round_trips(fills)
    trades, lookahead_units = [], []
    for r in rt.itertuples(index=False):
        seg = P[(P.t >= r.entry_t) & (P.t <= r.exit_t)]
        if len(seg) < 30 or (r.exit_t - r.entry_t).total_seconds() / 60 < a.min_hold:
            continue
        i0 = int(np.searchsorted(uts, pd.Timestamp(r.entry_t).value))
        pre = upx[max(0, i0 - a.vol_win):i0]          # STRICTLY before the entry
        u = causal_unit(pre)
        if u <= 0:
            continue
        adv = r.dir * (seg.price.to_numpy(float) - r.entry)
        mins = (pd.DatetimeIndex(seg.t) - r.entry_t).total_seconds().to_numpy() / 60
        risk = max(abs(float(np.min(adv[:min(len(adv), 60)]))), u)
        trades.append({"adv": adv, "mins": mins, "unit": u, "risk": risk,
                       "actual": r.dir * (r.exit - r.entry),
                       "oracle": float(adv.max())})
        # what the OLD (lookahead) method would have said, for comparison
        j1 = int(np.searchsorted(uts, pd.Timestamp(r.exit_t).value))
        lookahead_units.append(causal_unit(upx[max(0, i0 - a.vol_win):j1]))
    if not trades:
        raise SystemExit("no usable trades")

    mult = np.full(len(trades), PU[a.symbol])
    act = np.array([t["actual"] for t in trades])
    ora = np.array([t["oracle"] for t in trades])
    big = np.argsort(-(ora * mult))[:5]
    base = float((act * mult).sum())
    act_tail = float((act * mult)[big].sum())
    cu = np.array([t["unit"] for t in trades])
    lu = np.array(lookahead_units)

    print("\n" + "=" * 86)
    print(f"CAUSAL-UNIT SWEEP — {a.symbol}, {len(trades)} replay trades, "
          f"{a.grid} grid")
    print(f"unit = median |30-{a.grid} move| over the {a.vol_win} "
          f"{a.grid}s BEFORE entry")
    print("=" * 86)
    print(f"\n  causal unit    median {np.median(cu):7.3f}  (what a live sleeve knows)")
    print(f"  lookahead unit median {np.median(lu):7.3f}  (what the research used)")
    print(f"  the old method saw a unit {100*(np.median(lu)/max(np.median(cu),1e-9)-1):+.0f}% "
          f"different -- and it could only know it AFTER the fact")
    print(f"\n  actual {base:+11,.0f}$   tail-5 {act_tail:+,.0f}$")
    print(f"\n  'pt' is the median arming distance in ES points (arm_mult x unit)"
          f" — the number\n  that actually decides when the ride phase ends.")
    print(f"\n{'arm':>7}{'pt':>7}{'reversal':>14}{'armed':>7}{'total $':>12}"
          f"{'vs actual':>11}{'TAIL-5 $':>11}{'vs act tail':>12}")
    best = None
    for k in ARMS:
        for rk, rf in REVS:
            v = np.array([run_trade(t["adv"], t["mins"], t["unit"], t["risk"],
                                    "range", float(k), rk, rf) for t in trades])
            narm = sum(1 for t in trades if float(np.max(t["adv"])) >= k * t["unit"])
            tot = float((v * mult).sum())
            tail = float((v * mult)[big].sum())
            mark = ("  <<< BOTH" if tot > base and tail > act_tail else
                    "  tail" if tail > act_tail else "  total" if tot > base else "")
            print(f"{k:>6}x{k*np.median(cu):7.1f}{f'{rk} {rf:g}':>14}{narm:7d}"
                  f"{tot:+12,.0f}{tot-base:+11,.0f}{tail:+11,.0f}"
                  f"{tail-act_tail:+12,.0f}{mark}")
            if best is None or tot > best[2]:
                best = (k, f"{rk} {rf:g}", tot)
    print(f"\n  best on this dataset: arm {best[0]}x + {best[1]}  ({best[2]:+,.0f}$)")
    print("  If the best multiplier moves between contracts, 12x was fitted to ES")
    print("  and the 'session-adaptive' unit is not adaptive enough.")
    print("=" * 86 + "\n")


if __name__ == "__main__":
    main()
