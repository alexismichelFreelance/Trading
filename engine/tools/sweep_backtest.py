"""Realistic replay P&L for the sweep-following signal.

The replication study measured EDGE PER CONTRACT, volume-weighted over the
sweep's own fills. That is the right number for "is there information here" and
the wrong number for "what would we make", for two reasons:

  1. Volume weighting. A 300-lot sweep contributes 300x a 1-lot to the average,
     but we trade ONE contract either way. The tradeable number is per EVENT.
  2. Timing. The study priced each fill at its own price -- i.e. it assumed you
     were inside the sweep. You cannot be: the sweep is how you LEARN a sweep
     happened. Realistically you enter after the last fill, having already
     missed the move through the book.

So this re-measures the same signal the way a sleeve would actually experience
it: cluster the tape (1ms gap + side flip -- no order_id needed, see
tools/sweep_proxy_test.py, 99.84% agreement with the MBO truth), wait for the
cluster to END, enter one lot at the next available price, hold H seconds, exit.

Costs are explicit and charged in ticks, not hand-waved: `--entry-slip` for
crossing to get in and `--exit-slip` for getting out. Default 1 tick each way
(0.5pt round trip) is the pessimistic case where both sides are aggressive; an
exit that rests costs less, so --exit-slip 0 brackets the optimistic end.

    .venv/Scripts/python.exe tools/sweep_backtest.py --days 20
    .venv/Scripts/python.exe tools/sweep_backtest.py --exit-slip 0
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB                     # noqa: E402
from tools.flow_replication import session_days                 # noqa: E402
from tools.sweep_replication import load_day                    # noqa: E402

TICK = 0.25
SPANS = [3, 4, 6, 8, 10]
# Widened: the live sleeve holds 15s and 55% of its exits are the TIMEOUT,
# i.e. the target almost never lands inside the window. So test whether the
# window is simply wrong before redesigning the exit.
HOLDS = [2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300]


def clusters(ts_ns, side, px, sz, dt_ms=1):
    """Tape-only sweep reconstruction: new cluster on side flip or gap > dt."""
    gap = np.diff(ts_ns, prepend=ts_ns[0]) > dt_ms * 1_000_000
    flip = np.concatenate([[True], side[1:] != side[:-1]])
    gid = np.cumsum(gap | flip)
    d = pd.DataFrame({"g": gid, "p": px, "s": sz,
                      "t": ts_ns, "d": np.where(side == "B", 1, -1)})
    a = d.groupby("g").agg(pmin=("p", "min"), pmax=("p", "max"),
                           last_px=("p", "last"), t_end=("t", "last"),
                           lots=("s", "sum"), dirn=("d", "first"))
    a["span"] = ((a["pmax"] - a["pmin"]) / TICK).round().astype(int)
    return a


def backtest(df: pd.DataFrame, entry_slip: float, exit_slip: float,
             direction: int = 1):
    """direction=+1 FOLLOW the sweep, -1 FADE it. Fading is not simply the
    negative of following: both sides still PAY the slippage, so the costs do
    not flip sign with the trade. That is why this has to be measured rather
    than inferred from the follow numbers."""
    ts_ns = pd.to_datetime(df["ts_recv"]).astype("int64").to_numpy()
    px = df["price"].to_numpy(float)
    sz = df["size"].to_numpy(float)
    side = df["side"].to_numpy()
    cl = clusters(ts_ns, side, px, sz)

    sec = ts_ns // 1_000_000_000
    idx = sec - sec[0]
    n = int(idx[-1]) + 1
    last = np.full(n, np.nan)
    last[idx] = px
    path = pd.Series(last).ffill().to_numpy()

    out = {}
    for S in SPANS:
        c = cl[cl["span"] >= S]
        if len(c) < 5:
            continue
        e_idx = (c["t_end"].to_numpy() // 1_000_000_000) - sec[0]
        sweep_dir = c["dirn"].to_numpy()
        dirn = sweep_dir * direction          # our side, not the sweep's
        # enter AFTER the sweep, paying to cross in OUR direction
        entry = c["last_px"].to_numpy() + dirn * entry_slip * TICK
        for H in HOLDS:
            fut = np.minimum(e_idx + H, n - 1)
            exit_px = path[fut] - dirn * exit_slip * TICK
            pnl = dirn * (exit_px - entry)
            out[(S, H)] = pnl
    return out


def run(symbol: str, ndays: int, entry_slip: float, exit_slip: float,
        direction: int) -> None:
    qdb = QuestDB(timeout=240.0)
    days = session_days(symbol, None, None)[-ndays:]
    acc: dict[tuple[int, int], list[np.ndarray]] = {}
    nsess = 0
    for d in days:
        try:
            df = load_day(qdb, symbol, d)
        except Exception as ex:                        # noqa: BLE001
            print(f"  {d}: load failed ({str(ex)[:50]})", flush=True)
            continue
        if len(df) < 50_000:
            continue
        r = backtest(df, entry_slip, exit_slip, direction)
        if not r:
            continue
        nsess += 1
        for k, v in r.items():
            acc.setdefault(k, []).append(v)
        print(f"  {d}: {len(df):,} fills", flush=True)

    if not nsess:
        raise SystemExit("no sessions")

    print("\n" + "=" * 92)
    mode = "FOLLOW" if direction > 0 else "FADE"
    print(f"SWEEP-{mode} REALISTIC REPLAY — {symbol}, {nsess} sessions, 1 lot/signal")
    print(f"entry after the sweep completes, crossing {entry_slip} tick; "
          f"exit {exit_slip} tick")
    print("per-EVENT P&L (not volume-weighted): this is what a 1-lot sleeve gets")
    print("=" * 92)
    print(f"\n{'span>=':>7} {'hold':>6} {'sig/day':>8} {'win%':>6} {'mean pt':>9} "
          f"{'med pt':>8} {'pt/day':>9} {'$/day':>9} {'sign%':>7}")
    for S in SPANS:
        for H in HOLDS:
            if (S, H) not in acc:
                continue
            per_day = acc[(S, H)]
            allp = np.concatenate(per_day)
            daily = np.array([p.sum() for p in per_day])
            sig = np.median([len(p) for p in per_day])
            hit = float(max((daily > 0).mean(), (daily < 0).mean()) * 100)
            print(f"{S:>7} {H:>5}s {sig:>8.0f} {100*(allp>0).mean():5.1f}% "
                  f"{allp.mean():+9.4f} {np.median(allp):+8.4f} "
                  f"{np.median(daily):+9.2f} {np.median(daily)*50:+9.0f} {hit:6.1f}%")
    print("\n  pt/day is the MEDIAN session total at 1 lot. sign% is the share of")
    print("  sessions with the same sign -- consistency matters more than the mean.")
    print("=" * 92 + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ESM5")
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--entry-slip", type=float, default=1.0, help="ticks")
    ap.add_argument("--exit-slip", type=float, default=1.0, help="ticks")
    ap.add_argument("--fade", action="store_true",
                    help="trade AGAINST the sweep instead of with it")
    a = ap.parse_args()
    run(a.symbol, a.days, a.entry_slip, a.exit_slip, -1 if a.fade else 1)


if __name__ == "__main__":
    main()
