"""Out-of-sample exit test — the rules found on 2 live sessions, judged on 15.

tools/exit_rule_test.py found, on 29 live trades:
    time 30m           -$5,862 -> +$34,780   (oracle median hold was 29.7 min)
    support_lost 0.25  +$11,525 at a 60.1 min median hold, against `time 60m`'s
                       +$4,392 at 60.0 min -- i.e. +$7,133 that is NOT explained
                       by exiting earlier.

Two sessions is not evidence. This re-runs the identical rules against trades
produced by tools/portfolio_replay.py over 15 different ESM5 sessions -- a
different contract and a different regime from the live days the rules came
from. Nothing is refitted: the same thresholds, the same causal construction.

If `time 30m` was ESM5's rhythm rather than a property of the sleeves, it will
not survive. If `support_lost` keeps its edge over a timing-matched clock, the
roster signal is real.

    .venv/Scripts/python.exe tools/exit_oos.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.exit_oracle import round_trips                       # noqa: E402
from tools.exit_rule_test import apply_rules                    # noqa: E402
from tools.exit_signal_search import features, position_grid    # noqa: E402

PU = {"ESM5": 50.0, "ESH5": 50.0, "ES": 50.0, "NQ": 20.0}


def load_prices(symbol: str) -> pd.DataFrame:
    """Trade prices for the replayed sessions, from the replay event cache."""
    rows = []
    for f in sorted((ROOT / ".cache" / "replay").glob(f"{symbol}_*.npz")):
        z = np.load(f)
        kind = z["kind"]
        m = kind == 1                       # trades only
        rows.append(pd.DataFrame({"ts": z["ts"][m], "price": z["a"][m]}))
    if not rows:
        raise SystemExit(f"no cached replay events for {symbol}")
    d = pd.concat(rows, ignore_index=True).sort_values("ts")
    d["t"] = pd.to_datetime(d["ts"], utc=True)
    return d.reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ESM5")
    ap.add_argument("--fills", default=None)
    ap.add_argument("--min-hold", type=float, default=2.0)
    ap.add_argument("--min-peak", type=int, default=2)
    ap.add_argument("--min-fe", type=float, default=2.0)
    ap.add_argument("--arm-min", type=float, default=1.0)
    a = ap.parse_args()
    a.support_fracs = [0.25, 0.5, 0.75]
    a.support_floors = [1, 2, 3]
    a.agree_levels = [0.0, 0.25, 0.5]
    a.oppose_ks = [1, 2, 3]
    a.giveback_fracs = [0.25, 0.5]
    a.times = [5, 15, 30, 60]

    fp = Path(a.fills) if a.fills else ROOT / ".cache" / f"replay_fills_{a.symbol}.csv"
    f = pd.read_csv(fp)
    f["t"] = pd.to_datetime(f["ts"], utc=True)
    px = {a.symbol: load_prices(a.symbol)}
    # replay fills are labelled 'ESM5:sleeve'; the price key is the contract
    f["symbol"] = a.symbol

    rt = round_trips(f)
    grid = position_grid(f, f["t"].min(), f["t"].max() + pd.Timedelta(hours=1))

    rows, syms, holds = [], [], []
    for r in rt.itertuples(index=False):
        p = px[a.symbol]
        seg = p[(p.t >= r.entry_t) & (p.t <= r.exit_t)]
        if len(seg) < 20 or (r.exit_t - r.entry_t).total_seconds() / 60 < a.min_hold:
            continue
        fr = features(r, seg.reset_index(drop=True), grid)
        pnl, hold = apply_rules(fr, a)
        rows.append(pnl); holds.append(hold); syms.append(a.symbol)
    if not rows:
        raise SystemExit("no usable trades")

    D, H = pd.DataFrame(rows), pd.DataFrame(holds)
    mult = pd.Series(syms).map(PU).to_numpy()
    base = float((D["actual"].to_numpy() * mult).sum())
    orac = float((D["ORACLE"].to_numpy() * mult).sum())

    print("\n" + "=" * 84)
    print(f"OUT-OF-SAMPLE EXIT TEST — {a.symbol}, {len(D)} replay trades, "
          f"{rt.sleeve.nunique()} sleeves")
    print("same rules, same thresholds, nothing refitted.")
    print("=" * 84)
    print(f"\n  actual  {base:+11,.0f}$     ORACLE ceiling {orac:+11,.0f}$")
    print(f"\n{'rule':>28}{'total $':>12}{'vs actual':>12}{'% oracle':>10}"
          f"{'win%':>7}{'med hold':>10}")
    res = []
    for c in D.columns:
        if c == "ORACLE":
            continue
        v = D[c].to_numpy() * mult
        res.append((c, v.sum(), 100 * (D[c] > 0).mean(),
                    float(np.median(H[c])) if c in H else float("nan")))
    for c, tot, win, hld in sorted(res, key=lambda x: -x[1]):
        star = "  <<<" if c != "actual" and tot > base else ""
        print(f"{c:>28}{tot:+12,.0f}{tot-base:+12,.0f}"
              f"{100*tot/orac if orac else 0:9.1f}%{win:6.0f}%{hld:10.1f}{star}")
    print(f"\n  ORACLE med hold {float(np.median(H['ORACLE'])):.1f} min")
    print("=" * 84 + "\n")


if __name__ == "__main__":
    main()
