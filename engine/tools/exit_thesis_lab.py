"""Do EXIT SIGNALS beat exit insurance?

tools/exit_lab.py compared stops, trails and scale-outs. Those are all
insurance -- what you do when you are wrong. None of them answers "why am I
still in this trade?", and none is an exit strategy.

This compares real exit REASONS on identical entries:

  T  target    the fade predicts price gives back the span the sweep just
               covered, so the exit is the sweep's OWN ORIGIN, scaled by the
               signal rather than by a tick count picked by hand.
  I  resweep   another deep sweep the SAME way as the one we faded means it was
               ignition, not exhaustion. The reason we are in the trade is gone,
               so leave -- do not wait for the stop to say so.
  T+I          both, whichever fires first.

against the two baselines:

  clock        hold to the horizon (what sweep_backtest.py actually measured)
  insurance    horizon + the 8-tick stop the shipped sleeve added untested

O (a peer signalling against us) is NOT measurable here: it needs other sleeves
running against the same tape, i.e. a full portfolio replay. Stated rather than
faked -- this tool measures T and I only.

Evaluated tick by tick on real CME MBO paths, both contracts, per session.

    .venv/Scripts/python.exe tools/exit_thesis_lab.py --symbol ESM5 --days 11
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
MIN_SPAN = 6
HOLD_S = 15
STOP_TICKS = 8.0            # the shipped, untested stop
ENTRY_SLIP = EXIT_SLIP = 1.0
FRACS = [0.5, 1.0, 1.5]     # retrace fraction of the sweep span


def _trades(df: pd.DataFrame, hold_s: int):
    """Sweep-fade entries + the tick path, the sweep's span, and the timestamp
    of the next SAME-DIRECTION deep sweep (the invalidation)."""
    ts = pd.to_datetime(df["ts_recv"]).astype("int64").to_numpy()
    px = df["price"].to_numpy(float)
    sz = df["size"].to_numpy(float)
    side = df["side"].to_numpy()
    cl = clusters(ts, side, px, sz)
    deep = cl[cl["span"] >= MIN_SPAN]
    if len(deep) < 5:
        return []
    d_ts = deep["t_end"].to_numpy()
    d_dir = deep["dirn"].to_numpy()
    out = []
    for k in range(len(deep)):
        t_end = d_ts[k]
        sweep_dir = int(d_dir[k])
        d = -sweep_dir                                   # FADE
        span_px = float(deep["pmax"].iloc[k] - deep["pmin"].iloc[k])
        i0 = int(np.searchsorted(ts, t_end, side="right"))
        i1 = int(np.searchsorted(ts, t_end + hold_s * 1_000_000_000, side="right"))
        if i1 <= i0:
            continue
        entry = float(deep["last_px"].iloc[k]) + d * ENTRY_SLIP * TICK
        # I: first later deep sweep in the ORIGINAL direction, inside the hold
        nxt = d_ts[(np.arange(len(deep)) > k) & (d_dir == sweep_dir)]
        inval = int(np.searchsorted(ts[i0:i1], nxt[0]) ) if len(nxt) else -1
        if len(nxt) and nxt[0] > ts[i1 - 1]:
            inval = -1                                   # invalidation after the horizon
        out.append((d, entry, px[i0:i1], span_px, inval))
    return out


def _exits(d, entry, path, span_px, inval):
    adv = d * (path - entry)
    cost = EXIT_SLIP * TICK
    stop_lim = -STOP_TICKS * TICK
    res = {}

    res[("clock", 0)] = adv[-1] - cost

    hit_stop = np.flatnonzero(adv <= stop_lim)
    j_stop = hit_stop[0] if len(hit_stop) else None
    res[("insurance", 0)] = (stop_lim if j_stop is not None else adv[-1]) - cost

    # I -- invalidation: leave at the re-sweep, stop still armed before it
    if inval >= 0:
        j = inval if j_stop is None else min(inval, j_stop)
        v = stop_lim if (j_stop is not None and j_stop <= inval) else adv[min(j, len(adv) - 1)]
    else:
        v = stop_lim if j_stop is not None else adv[-1]
    res[("resweep", 0)] = v - cost

    for f in FRACS:                                      # T -- thesis complete
        tgt = f * span_px
        ht = np.flatnonzero(adv >= tgt)
        j_t = ht[0] if len(ht) else None
        if j_t is not None and (j_stop is None or j_t <= j_stop):
            res[("target", f)] = tgt - cost
        elif j_stop is not None:
            res[("target", f)] = stop_lim - cost
        else:
            res[("target", f)] = adv[-1] - cost
        # T+I -- whichever reason fires first
        cands = [x for x in (j_t, j_stop, inval if inval >= 0 else None) if x is not None]
        if not cands:
            res[("target+resweep", f)] = adv[-1] - cost
        else:
            j = min(cands)
            if j_t is not None and j == j_t:
                res[("target+resweep", f)] = tgt - cost
            elif j_stop is not None and j == j_stop:
                res[("target+resweep", f)] = stop_lim - cost
            else:
                res[("target+resweep", f)] = adv[min(j, len(adv) - 1)] - cost
    return res


def run(symbol: str, ndays: int, hold_s: int) -> None:
    qdb = QuestDB(timeout=240.0)
    days = session_days(symbol, None, None)[-ndays:]
    acc: dict[tuple, list[float]] = {}
    daily: dict[tuple, list[float]] = {}
    nsess = ntr = ninval = 0
    for dd in days:
        try:
            df = load_day(qdb, symbol, dd)
        except Exception as ex:                          # noqa: BLE001
            print(f"  {dd}: load failed ({str(ex)[:45]})", flush=True)
            continue
        if len(df) < 50_000:
            continue
        tr = _trades(df, hold_s)
        if not tr:
            continue
        nsess += 1
        ntr += len(tr)
        ninval += sum(1 for t in tr if t[4] >= 0)
        day_acc: dict[tuple, float] = {}
        for d, entry, path, span_px, inval in tr:
            for k, v in _exits(d, entry, path, span_px, inval).items():
                acc.setdefault(k, []).append(v)
                day_acc[k] = day_acc.get(k, 0.0) + v
        for k, v in day_acc.items():
            daily.setdefault(k, []).append(v)
        print(f"  {dd}: {len(tr)} sweeps", flush=True)

    if not acc:
        raise SystemExit("no trades")

    print("\n" + "=" * 84)
    print(f"EXIT THESIS LAB — {symbol}, {nsess} sessions, {ntr} entries "
          f"(span>={MIN_SPAN}, horizon {hold_s}s)")
    print(f"invalidated within the horizon: {ninval}/{ntr} "
          f"({100*ninval/max(ntr,1):.1f}%)")
    print("identical entries; only the EXIT REASON differs. per-trade points.")
    print("=" * 84)
    print(f"\n{'exit reason':>20} {'win%':>6} {'mean':>8} {'median':>8} "
          f"{'pt/day':>8} {'sign%':>7}  vs clock")
    base = np.mean(acc[("clock", 0)])
    order = [("clock", 0), ("insurance", 0), ("resweep", 0)] + \
            [("target", f) for f in FRACS] + [("target+resweep", f) for f in FRACS]
    for k in order:
        if k not in acc:
            continue
        a = np.array(acc[k]); dl = np.array(daily[k])
        hit = float(max((dl > 0).mean(), (dl < 0).mean()) * 100)
        nm = k[0] if not k[1] else f"{k[0]} {k[1]:g}x"
        print(f"{nm:>20} {100*(a>0).mean():5.1f}% {a.mean():+8.4f} "
              f"{np.median(a):+8.4f} {np.median(dl):+8.2f} {hit:6.1f}% "
              f"{a.mean()-base:+8.4f}")
    print("\n  'clock' is what sweep_backtest actually measured. 'insurance' is")
    print("  what the sleeve shipped with. Everything below them is an exit")
    print("  REASON -- it has to beat both to justify existing.")
    print("=" * 84 + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ESM5")
    ap.add_argument("--days", type=int, default=11)
    ap.add_argument("--hold", type=int, default=HOLD_S)
    a = ap.parse_args()
    run(a.symbol, a.days, a.hold)


if __name__ == "__main__":
    main()
