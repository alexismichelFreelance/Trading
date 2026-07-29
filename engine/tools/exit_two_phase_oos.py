"""Out-of-sample test of the two-phase exit.

In-sample (29 live ES trades, 2 sessions) the best pair was:

    arm  = MFE >= 12 x the session's own typical 30-bar move
    exit = momentum decay (recent advance rate <= 0.1 x the rate that made the peak)

    total   -$3,755 -> +$11,848   (+$15,602)
    tail-5  +$22,065 -> +$35,890  (+$13,825)
    armed   8 of 29 trades; the other 21 rode untouched

That is the same shape of evidence that produced `time 30m`, which looked just
as convincing and then died out-of-sample. So it gets the same test: the SAME
thresholds, refitted to nothing, against trades from 15 replayed ESM5 sessions
(tools/portfolio_replay.py) -- a different contract, a different year, a
different regime.

The two numbers that decide it:
  * does the TAIL still improve? that is the whole thesis -- protect the moves
    that pay instead of capping them.
  * does the total stay positive vs actual? if the tail improves but the total
    collapses, the machinery is hurting the trades it should not be touching.

    .venv/Scripts/python.exe tools/exit_two_phase_oos.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.features.swings import vol_unit          # noqa: E402
from tools.exit_oracle import round_trips            # noqa: E402
from tools.exit_two_phase import run_trade           # noqa: E402

PU = {"ESM5": 50.0, "ESH5": 50.0}


def load_replay(symbol: str):
    """Replay fills + the tick path + each session's own volatility unit."""
    fills = pd.read_csv(ROOT / ".cache" / f"replay_fills_{symbol}.csv")
    fills["t"] = pd.to_datetime(fills["ts"], utc=True)
    fills["symbol"] = symbol
    px, units = [], {}
    for f in sorted((ROOT / ".cache" / "replay").glob(f"{symbol}_*.npz")):
        z = np.load(f)
        m = z["kind"] == 1
        d = pd.DataFrame({"ts": z["ts"][m], "price": z["a"][m]})
        px.append(d)
        day = f.stem.split("_", 1)[1]
        # same unit definition as in-sample: median |move| over 30 samples,
        # taken on a 1-per-second view so it is comparable to the bar version
        p = d["price"].to_numpy(float)
        step = max(1, len(p) // 20000)
        units[day] = float(vol_unit(p[::step], horizon=30) or 0.0)
    P = pd.concat(px, ignore_index=True).sort_values("ts").reset_index(drop=True)
    P["t"] = pd.to_datetime(P["ts"], utc=True)
    return fills, P, units


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ESM5")
    ap.add_argument("--min-hold", type=float, default=2.0)
    a = ap.parse_args()

    fills, P, units = load_replay(a.symbol)
    rt = round_trips(fills)
    trades = []
    for r in rt.itertuples(index=False):
        seg = P[(P.t >= r.entry_t) & (P.t <= r.exit_t)]
        if len(seg) < 30 or (r.exit_t - r.entry_t).total_seconds() / 60 < a.min_hold:
            continue
        day = r.entry_t.tz_convert("America/New_York").strftime("%Y-%m-%d")
        u = units.get(day, 0.0)
        if u <= 0:
            continue
        adv = r.dir * (seg.price.to_numpy(float) - r.entry)
        mins = (pd.DatetimeIndex(seg.t) - r.entry_t).total_seconds().to_numpy() / 60
        risk = max(abs(float(np.min(adv[:min(len(adv), 60)]))), u)
        trades.append({"adv": adv, "mins": mins, "unit": u, "risk": risk,
                       "actual": r.dir * (r.exit - r.entry),
                       "oracle": float(adv.max()), "sleeve": r.sleeve})
    if not trades:
        raise SystemExit("no usable replay trades")

    mult = np.full(len(trades), PU[a.symbol])
    act = np.array([t["actual"] for t in trades])
    ora = np.array([t["oracle"] for t in trades])
    big = np.argsort(-(ora * mult))[:5]
    base, orac = float((act * mult).sum()), float((ora * mult).sum())
    act_tail = float((act * mult)[big].sum())
    ora_tail = float((ora * mult)[big].sum())

    print("\n" + "=" * 88)
    print(f"TWO-PHASE EXIT — OUT OF SAMPLE — {a.symbol}, {len(trades)} replay trades")
    print("identical thresholds, refitted to nothing")
    print("=" * 88)
    print(f"\n  actual {base:+11,.0f}$   ORACLE {orac:+11,.0f}$")
    print(f"  actual tail-5 {act_tail:+,.0f}$ of a {ora_tail:+,.0f}$ ceiling "
          f"({100*ora_tail/orac:.0f}% of the prize)")
    print(f"\n{'arm':>14}{'reversal':>12}{'armed':>7}{'total $':>12}"
          f"{'vs actual':>11}{'TAIL-5 $':>11}{'vs act tail':>12}")
    rows = []
    for ak, akk in (("range", 8.0), ("range", 12.0), ("risk", 5.0), ("risk", 8.0)):
        for rk, rf in (("decay", 0.1), ("range", 0.25), ("retrace", 0.25),
                       ("stall", 1.0)):
            v = np.array([run_trade(t["adv"], t["mins"], t["unit"], t["risk"],
                                    ak, akk, rk, rf) for t in trades])
            narm = sum(1 for t in trades
                       if float(np.max(t["adv"])) >=
                       (akk * t["unit"] if ak == "range" else akk * t["risk"]))
            rows.append((f"{ak} {akk:g}x", f"{rk} {rf:g}",
                         float((v * mult).sum()), float((v * mult)[big].sum()), narm))
    for arm, rev, tot, tail, narm in sorted(rows, key=lambda x: -x[2]):
        mark = ("  <<< BOTH" if tot > base and tail > act_tail else
                "  tail" if tail > act_tail else
                "  total" if tot > base else "")
        print(f"{arm:>14}{rev:>12}{narm:7d}{tot:+12,.0f}{tot-base:+11,.0f}"
              f"{tail:+11,.0f}{tail-act_tail:+12,.0f}{mark}")
    print("\n  IN-SAMPLE the headline pair was range 12x + decay 0.1:")
    print("  total +$15,602 vs actual, tail +$13,825, 8/29 armed.")
    print("  If those signs hold here, the mechanism generalises. If they flip,")
    print("  it was 29 trades of luck -- exactly like `time 30m`.")
    print("=" * 88 + "\n")


if __name__ == "__main__":
    main()
