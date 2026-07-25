"""Replication study, part 2: is AGGRESSIVENESS a better filter than size?

Part 1 (tools/flow_replication.py) showed copying every trade loses, and that
filtering by trade size selects informed flow: the 1-second edge per contract is
monotonic in size at 95% sign consistency across sessions. But size is a crude
proxy. A single 100-lot can be a passive rebalance with no information in it,
while one order tearing through four price levels is somebody working an order
urgently. Those should not be filtered the same way.

CME MBO makes the distinction exact rather than heuristic: on a trade message the
order_id is the AGGRESSING order, so one aggressive order that walks the book
appears as several fills at ascending prices sharing an order_id (verified in the
raw tape -- one order filling 5 levels inside a single microsecond). Grouping by
order_id reconstructs the parent order, and with it:

    order_size     how much the aggressor actually wanted
    ticks_spanned  how many price levels it was willing to pay through
    nfills         how fragmented the fill was

Three ENTRY axes, one EXIT axis (hold horizon), same metric as part 1 -- edge
per contract in points, with the entry spread already inside the fills.

CRITICAL: the filter is applied at ORDER level but execution is at FILL level,
each fill at its own price. Copying a sweep means paying every level it paid,
not the first one. Part 1 filtered on per-FILL size, which is a different and
weaker quantity; order size is the honest version.

Also extends the size grid upward: part 1's optimum sat pinned at S=100, the
boundary of its grid, which means the real optimum was outside the box.

    .venv/Scripts/python.exe tools/sweep_replication.py --days 20
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB                    # noqa: E402
from tools.flow_replication import session_days                # noqa: E402


def load_day(qdb: QuestDB, symbol: str, day: str) -> pd.DataFrame:
    """Part 1's loader does not fetch order_id, and the parent-order grouping is
    the whole point here -- so this tool needs its own SELECT."""
    return qdb.df(
        "SELECT ts_recv, price, size, side, order_id FROM mbo_events "
        f"WHERE action='T' AND symbol='{symbol}' "
        f"AND ts_recv >= '{day}T00:00:00.000000Z' "
        f"AND ts_recv <  '{day}T23:59:59.999999Z' ORDER BY ts_recv")

TICK = 0.25
HORIZONS = [1, 5, 15, 60, 300, 900]
AXES = {
    "order_size": [1, 10, 50, 100, 250, 500, 1000],   # extended past part 1's edge
    "ticks_spanned": [0, 1, 2, 3, 4, 6, 8],
    "nfills": [1, 2, 3, 5, 8, 12],
}


def order_attrs(df: pd.DataFrame) -> pd.DataFrame:
    """Reconstruct the parent aggressing order from its fills."""
    g = df.groupby("order_id", sort=False)
    a = g.agg(order_size=("size", "sum"), nfills=("size", "size"),
              pmin=("price", "min"), pmax=("price", "max"))
    a["ticks_spanned"] = ((a["pmax"] - a["pmin"]) / TICK).round().astype(int)
    return a[["order_size", "nfills", "ticks_spanned"]]


def replicate_axes(df: pd.DataFrame) -> dict[tuple[str, float, int], tuple[float, float]]:
    if len(df) < 1000:
        return {}
    attrs = order_attrs(df)
    j = df.join(attrs, on="order_id")

    ts = pd.to_datetime(j["ts_recv"]).astype("int64").to_numpy() // 1_000_000_000
    px = j["price"].to_numpy(float)
    sz = j["size"].to_numpy(float)
    sgn = np.where(j["side"].to_numpy() == "B", 1.0, -1.0)      # B = buy aggressor

    idx = ts - ts[0]
    n = int(idx[-1]) + 1
    last = np.full(n, np.nan)
    last[idx] = px
    path = pd.Series(last).ffill().to_numpy()

    out = {}
    for axis, grid in AXES.items():
        col = j[axis].to_numpy()
        for thr in grid:
            m = col >= thr
            if m.sum() < 50:
                continue
            vol = float(sz[m].sum())
            # distinct PARENT ORDERS, not fills: this is the real sample size
            # behind a row, and the thing that tells you whether a suspiciously
            # clean sign% is carried by a handful of events.
            norders = int(pd.Series(j["order_id"].to_numpy()[m]).nunique())
            for H in HORIZONS:
                fut = np.minimum(idx[m] + H, n - 1)
                pnl = float((sgn[m] * sz[m] * (path[fut] - px[m])).sum())
                out[(axis, thr, H)] = (pnl / vol, vol, norders)
    return out


def run(symbol: str, ndays: int, start: str | None, end: str | None) -> None:
    qdb = QuestDB(timeout=240.0)
    days = session_days(symbol, start, end)
    days = days[-ndays:] if ndays else days
    print(f"{symbol}: {len(days)} candidate sessions", flush=True)

    acc: dict[tuple[str, float, int], list[float]] = {}
    vols: dict[tuple[str, float], list[float]] = {}
    ords: dict[tuple[str, float], list[int]] = {}
    nsess = 0
    for i, d in enumerate(days):
        try:
            df = load_day(qdb, symbol, d)
        except Exception as ex:                          # noqa: BLE001
            print(f"  {d}: load failed ({str(ex)[:50]})", flush=True)
            continue
        if len(df) < 50_000:
            continue
        r = replicate_axes(df)
        if not r:
            continue
        nsess += 1
        for k, (edge, vol, norders) in r.items():
            acc.setdefault(k, []).append(edge)
            vols.setdefault((k[0], k[1]), []).append(vol)
            ords.setdefault((k[0], k[1]), []).append(norders)
        if i % 5 == 0:
            print(f"  {d}: {len(df):,} fills, "
                  f"{df['order_id'].nunique():,} parent orders", flush=True)

    if not nsess:
        raise SystemExit("no sessions produced results")

    print("\n" + "=" * 96)
    print(f"SWEEP vs SIZE REPLICATION — {symbol}, {nsess} sessions")
    print("edge per contract, POINTS. Entry spread is already inside the fills;")
    print("a round trip owes ~0.125pt more to exit across the book.")
    print("filter applied at PARENT-ORDER level, executed at every FILL's own price")
    print("=" * 96)

    for axis, grid in AXES.items():
        print(f"\n[{axis}]")
        hdr = "".join(f"{f'{H}s':>14}" for H in HORIZONS)
        print(f"{'>=':>8}{hdr}      lots/day   orders/day")
        for thr in grid:
            cells = []
            for H in HORIZONS:
                v = np.array(acc.get((axis, thr, H), []))
                if len(v) < 5:
                    cells.append(f"{'-':>14}")
                    continue
                med = float(np.median(v))
                hit = float(max((v > 0).mean(), (v < 0).mean()) * 100)
                cells.append(f"{med:+8.4f}({hit:3.0f}%)")
            vm = np.median(vols.get((axis, thr), [0]))
            om = np.median(ords.get((axis, thr), [0]))
            print(f"{thr:>8}" + "".join(cells) + f"  {vm:>11,.0f} {om:>11,.0f}")

    print("\n  Compare axes AT MATCHED lots/day -- a filter that keeps less volume")
    print("  should be judged against a size filter keeping the same volume, not")
    print("  against the whole tape. Sign% is what makes a cell real.")
    print("=" * 96 + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ESM5")
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    a = ap.parse_args()
    run(a.symbol, a.days, a.start, a.end)


if __name__ == "__main__":
    main()
