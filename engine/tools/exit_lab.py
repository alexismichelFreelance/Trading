"""Exit lab — do stops, trails or scale-outs actually beat holding to a clock?

Every sleeve in this engine had its ENTRY researched and its EXIT chosen by
feel. The sweep sleeve is the clearest case: tools/sweep_backtest.py measured a
+0.50pt median edge with NO STOP AT ALL (its exit is one line, `exit_px =
path[fut]`), and the shipped sleeve then added an 8-tick stop that was never
tested. On 2026-07-27 that stop fired on 3 of 5 trades. The sleeve trading in
paper is not the thing that passed validation.

This compares exit MECHANISMS on identical entries, on the real tick path:

  A  hold        exit at the horizon, no stop            (what was measured)
  B  stop        fixed stop, else horizon
  C  trail       stop trails the peak by a fixed distance
  D  scaleout    half off at +T, stop to breakeven on the rest, else horizon

All four are path-dependent, so they are evaluated tick by tick against real
CME MBO trades -- bars would hide the ordering of a stop and a target inside the
same minute, which is exactly where these mechanisms differ.

Distances are in TICKS and are swept, because the Layer-1 survey
(tools/swing_scales.py) says the moves a span>=6 sweep belongs to carry a median
adverse excursion of ~3.25pt at scale 4. A 2pt stop sits INSIDE the normal heat
of the move it is trading, which is not risk management, it is a filter that
converts winners into losers. The grid is wide enough to show that.

Entries come from the one signal with out-of-sample evidence: the tape-clustered
sweep fade (tools/sweep_proxy_test.py, 99.84% agreement with MBO order_id).

    .venv/Scripts/python.exe tools/exit_lab.py --days 20
    .venv/Scripts/python.exe tools/exit_lab.py --symbol ESH5 --days 20
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
from tools.sweep_backtest import clusters                       # noqa: E402
from tools.sweep_replication import load_day                    # noqa: E402

TICK = 0.25
MIN_SPAN = 6                 # the validated entry filter
HOLD_S = 15                  # the measured horizon
ENTRY_SLIP = 1.0             # ticks, crossing to get in
EXIT_SLIP = 1.0              # ticks, crossing to get out

STOPS = [4, 8, 12, 16, 24]           # ticks
TRAILS = [4, 8, 12, 16, 24]          # ticks behind the peak
TP = [4, 8, 12]                      # ticks, scale-out target for the first half


def _paths(df: pd.DataFrame, hold_s: int):
    """Entries from the sweep detector + the tick path following each one."""
    ts = pd.to_datetime(df["ts_recv"]).astype("int64").to_numpy()
    px = df["price"].to_numpy(float)
    sz = df["size"].to_numpy(float)
    side = df["side"].to_numpy()
    cl = clusters(ts, side, px, sz)
    c = cl[cl["span"] >= MIN_SPAN]
    if len(c) < 5:
        return []
    out = []
    for t_end, last_px, sweep_dir in zip(c["t_end"], c["last_px"], c["dirn"]):
        d = -int(sweep_dir)                       # FADE the sweep
        i0 = int(np.searchsorted(ts, t_end, side="right"))
        i1 = int(np.searchsorted(ts, t_end + hold_s * 1_000_000_000, side="right"))
        if i1 <= i0:
            continue
        entry = last_px + d * ENTRY_SLIP * TICK
        out.append((d, entry, px[i0:i1]))
    return out


def _exits(d, entry, path, stops, trails, tps):
    """P&L of every mechanism on ONE trade, in points, net of exit slippage.
    `adv` is the excursion in our favour; heat is negative adv."""
    adv = d * (path - entry)
    cost = EXIT_SLIP * TICK
    res = {}

    res[("hold", 0)] = adv[-1] - cost

    for s in stops:                                # first touch of -s ends it
        lim = -s * TICK
        hit = np.flatnonzero(adv <= lim)
        res[("stop", s)] = (lim if len(hit) else adv[-1]) - cost

    peak = np.maximum.accumulate(adv)
    for tr in trails:                              # trail from the running peak
        gap = tr * TICK
        hit = np.flatnonzero(adv <= peak - gap)
        res[("trail", tr)] = ((peak[hit[0]] - gap) if len(hit) else adv[-1]) - cost

    for tp in tps:                                 # half off at +tp, rest at BE
        tgt = tp * TICK
        ht = np.flatnonzero(adv >= tgt)
        if not len(ht):                            # target never reached
            res[("scaleout", tp)] = adv[-1] - cost
            continue
        j = ht[0]
        rest = adv[j:]
        be = np.flatnonzero(rest <= 0.0)           # breakeven stop on the remainder
        second = 0.0 if len(be) else rest[-1]
        res[("scaleout", tp)] = 0.5 * (tgt - cost) + 0.5 * (second - cost)
    return res


def run(symbol: str, ndays: int, hold_s: int) -> None:
    qdb = QuestDB(timeout=240.0)
    days = session_days(symbol, None, None)[-ndays:]
    acc: dict[tuple, list[float]] = {}
    daily: dict[tuple, list[float]] = {}
    nsess = ntr = 0
    for d in days:
        try:
            df = load_day(qdb, symbol, d)
        except Exception as ex:                    # noqa: BLE001
            print(f"  {d}: load failed ({str(ex)[:50]})", flush=True)
            continue
        if len(df) < 50_000:
            continue
        trades = _paths(df, hold_s)
        if not trades:
            continue
        nsess += 1
        ntr += len(trades)
        day_acc: dict[tuple, float] = {}
        for dirn, entry, path in trades:
            for k, v in _exits(dirn, entry, path, STOPS, TRAILS, TP).items():
                acc.setdefault(k, []).append(v)
                day_acc[k] = day_acc.get(k, 0.0) + v
        for k, v in day_acc.items():
            daily.setdefault(k, []).append(v)
        print(f"  {d}: {len(trades)} sweeps", flush=True)

    if not acc:
        raise SystemExit("no trades")

    print("\n" + "=" * 88)
    print(f"EXIT LAB — {symbol}, {nsess} sessions, {ntr} sweep-fade entries "
          f"(span>={MIN_SPAN}, hold {hold_s}s)")
    print(f"identical entries; only the EXIT differs. {ENTRY_SLIP:.0f} tick in, "
          f"{EXIT_SLIP:.0f} tick out. per-trade points.")
    print("=" * 88)
    print(f"\n{'mechanism':>16} {'win%':>6} {'mean':>8} {'median':>8} "
          f"{'pt/day':>8} {'sign%':>7}   vs hold")

    base = np.mean(acc[("hold", 0)])
    order = [("hold", 0)] + [("stop", s) for s in STOPS] + \
            [("trail", t) for t in TRAILS] + [("scaleout", t) for t in TP]
    for k in order:
        if k not in acc:
            continue
        a = np.array(acc[k])
        dl = np.array(daily[k])
        hit = float(max((dl > 0).mean(), (dl < 0).mean()) * 100)
        name = k[0] if k[0] == "hold" else f"{k[0]} {k[1]}t"
        delta = a.mean() - base
        print(f"{name:>16} {100*(a>0).mean():5.1f}% {a.mean():+8.4f} "
              f"{np.median(a):+8.4f} {np.median(dl):+8.2f} {hit:6.1f}% "
              f"{delta:+8.4f}")

    print("\n  'vs hold' is the whole point: a mechanism only earns its")
    print("  complexity if it beats simply waiting out the clock.")
    print("=" * 88 + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ESM5")
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--hold", type=int, default=HOLD_S)
    a = ap.parse_args()
    run(a.symbol, a.days, a.hold)


if __name__ == "__main__":
    main()
