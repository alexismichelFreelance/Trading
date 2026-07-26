"""Can a sweep be detected WITHOUT order_id -- i.e. from a plain trade tape?

This decides whether the sweep edge needs a new market-data subscription or is
already reachable with what the NT8 bridge delivers today.

NT8's OnMarketData gives (time, price, size) and an inferred aggressor, with no
order identity. CME MBO gives order_id, and on a trade message that id is the
AGGRESSING order -- so grouping by it reconstructs the parent order exactly.
That exactness is what the sweep study used.

But order_id may not be NECESSARY. A sweep is several fills at walking prices
within microseconds, and NT8 does deliver each fill as its own print. So the
pattern should be visible without the id; the id merely makes the grouping
certain. This measures how much that certainty is worth.

  TRUTH  group fills by order_id                       (MBO only)
  PROXY  start a new group when the side flips or the
         inter-trade gap exceeds dt                    (works on ANY tape)

Both groupings are scored the same way as the main study -- edge per contract at
a range of horizons for ticks_spanned filters -- and compared head to head on the
SAME fills. If the proxy tracks the truth, the live NT8 tape is enough and no
feed purchase is implied. If it does not, the gap is the price of order-level
data, quantified rather than asserted.

    .venv/Scripts/python.exe tools/sweep_proxy_test.py --days 5
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
HORIZONS = [1, 5, 15]
SPANS = [2, 3, 4, 6, 8]
DTS_MS = [0, 1, 5, 20, 100]        # proxy clustering windows


def proxy_groups(ts_ns: np.ndarray, side: np.ndarray, dt_ms: int) -> np.ndarray:
    """Group id from the tape alone: a new group starts when the aggressor side
    flips or the gap since the previous print exceeds dt."""
    gap = np.diff(ts_ns, prepend=ts_ns[0]) > dt_ms * 1_000_000
    flip = np.concatenate([[True], side[1:] != side[:-1]])
    return np.cumsum(gap | flip)


def span_of(groups: np.ndarray, px: np.ndarray) -> np.ndarray:
    """ticks_spanned of each fill's parent group, broadcast back to fills."""
    d = pd.DataFrame({"g": groups, "p": px})
    agg = d.groupby("g")["p"].agg(["min", "max"])
    sp = ((agg["max"] - agg["min"]) / TICK).round().astype(int)
    return d["g"].map(sp).to_numpy()


def edges(px, sz, sgn, idx, path, n, span, spans=SPANS, horizons=HORIZONS):
    out = {}
    for s in spans:
        m = span >= s
        if m.sum() < 50:
            continue
        vol = float(sz[m].sum())
        for H in horizons:
            fut = np.minimum(idx[m] + H, n - 1)
            out[(s, H)] = float((sgn[m] * sz[m] * (path[fut] - px[m])).sum()) / vol
    return out


def run(symbol: str, ndays: int) -> None:
    qdb = QuestDB(timeout=240.0)
    days = session_days(symbol, None, None)[-ndays:]
    truth: dict = {}
    prox: dict = {}
    agree: list[float] = []

    for d in days:
        try:
            df = load_day(qdb, symbol, d)
        except Exception as ex:                       # noqa: BLE001
            print(f"  {d}: load failed ({str(ex)[:50]})", flush=True)
            continue
        if len(df) < 50_000:
            continue
        ts_ns = pd.to_datetime(df["ts_recv"]).astype("int64").to_numpy()
        ts = ts_ns // 1_000_000_000
        px = df["price"].to_numpy(float)
        sz = df["size"].to_numpy(float)
        side = df["side"].to_numpy()
        sgn = np.where(side == "B", 1.0, -1.0)
        idx = ts - ts[0]
        n = int(idx[-1]) + 1
        last = np.full(n, np.nan)
        last[idx] = px
        path = pd.Series(last).ffill().to_numpy()

        sp_true = span_of(df["order_id"].to_numpy(), px)
        for k, v in edges(px, sz, sgn, idx, path, n, sp_true).items():
            truth.setdefault(k, []).append(v)

        for dt in DTS_MS:
            sp_p = span_of(proxy_groups(ts_ns, side, dt), px)
            for k, v in edges(px, sz, sgn, idx, path, n, sp_p).items():
                prox.setdefault((dt, *k), []).append(v)
            # how often does the proxy agree with truth on "is this a >=4 sweep"?
            agree.append(float(((sp_p >= 4) == (sp_true >= 4)).mean()) if dt == 1 else np.nan)
        print(f"  {d}: {len(df):,} fills", flush=True)

    if not truth:
        raise SystemExit("no sessions")

    print("\n" + "=" * 84)
    print(f"SWEEP DETECTION WITHOUT order_id — {symbol}, {len(truth[list(truth)[0]])} sessions")
    print("edge per contract (points), median across sessions")
    print("=" * 84)
    hdr = "".join(f"{f'{H}s':>12}" for H in HORIZONS)
    print(f"\n{'grouping':>22}{hdr}")
    for s in SPANS:
        print(f"\n  ticks_spanned >= {s}")
        row = [np.median(truth[(s, H)]) if (s, H) in truth else np.nan for H in HORIZONS]
        print(f"{'order_id (TRUTH)':>22}" + "".join(f"{v:+12.4f}" for v in row))
        for dt in DTS_MS:
            r = [np.median(prox[(dt, s, H)]) if (dt, s, H) in prox else np.nan
                 for H in HORIZONS]
            print(f"{f'tape proxy dt={dt}ms':>22}" + "".join(f"{v:+12.4f}" for v in r))
    a = np.array([x for x in agree if np.isfinite(x)])
    if len(a):
        print(f"\nper-fill agreement on 'is a >=4-tick sweep' (dt=1ms): "
              f"{100*a.mean():.2f}%")
    print("\nIf the proxy rows track the truth row, order_id is a convenience, not")
    print("a requirement, and the NT8 tape already carries this signal.")
    print("=" * 84 + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ESM5")
    ap.add_argument("--days", type=int, default=5)
    a = ap.parse_args()
    run(a.symbol, a.days)


if __name__ == "__main__":
    main()
