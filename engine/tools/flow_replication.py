"""Replication study: if you copied the market's own aggressive trades, would
you make money -- and which subset should you copy?

This is the question flow was built to answer, stated properly. Not "does a flow
indicator predict returns", but: take real trades at their REAL execution prices,
hold the resulting position, and mark it out. Copying an aggressor means buying
at the ask and selling at the bid, so the spread is already inside the fill
prices -- it is paid whether or not commissions exist. The edge, if any, has to
come from price impact that PERSISTS beyond that cost.

The two axes are the two problems, kept separate because they are separate:

  ENTRY  which trades to copy      -> size threshold S (a grid, not a guess)
  EXIT   how long to hold them     -> horizon H (a grid)

Metric is EDGE PER CONTRACT, in points:

    edge(S,H) = SUM over selected trades of  s_i * q_i * (p_{t_i+H} - p_i)
                --------------------------------------------------------
                             SUM of q_i

Per-contract normalisation is what makes filters comparable: a bigger S copies
fewer contracts, so raw P&L would fall for a filter that is actually BETTER.

Reading the H axis is the mechanism test. Price impact splits into a permanent
part (information -- price never comes back) and a transient part (liquidity
absorption -- it reverts). So:
    edge rising with H      -> the selected flow is informed. Permanent impact.
    edge peaking then decaying -> transient. You are renting the move, not owning it.
    edge negative           -> you are the liquidity the informed side is using.

Everything is computed PER SESSION and reported as a distribution across
sessions, never as one number over months: the question is whether the best
(S,H) CLUSTERS and how it DRIFTS, which an average would destroy.

Data: mbo_events, real CME MBO trade messages (action='T'), ~44.5M rows over
Feb-May 2025. side='B' is the BUY AGGRESSOR -- verified empirically, not assumed
(signed volume correlates +0.53 with contemporaneous price change; the opposite
convention gives exactly -0.53).

    .venv/Scripts/python.exe tools/flow_replication.py
    .venv/Scripts/python.exe tools/flow_replication.py --symbol ESH5 --days 30
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB          # noqa: E402

SIZES = [1, 2, 5, 10, 25, 50, 100]          # ENTRY grid: copy trades >= S lots
HORIZONS = [1, 5, 15, 60, 300, 900]         # EXIT grid: hold H seconds
TICK = 0.25


# Contract date ranges. mbo_events is ~78GB, so ANY full-table aggregate (even
# `SAMPLE BY 1d`) blows QuestDB's 60s server-side query timeout. Enumerating
# candidate weekdays and hitting one day-partition at a time keeps every query
# bounded; days with no data simply come back empty and are skipped.
RANGES = {"ESH5": ("2025-02-18", "2025-03-19"),
          "ESM5": ("2025-03-20", "2025-05-30")}


def session_days(symbol: str, start: str | None, end: str | None) -> list[str]:
    lo, hi = RANGES.get(symbol, ("2025-02-18", "2025-05-30"))
    days = pd.bdate_range(start or lo, end or hi)
    return [d.strftime("%Y-%m-%d") for d in days]


def load_day(qdb: QuestDB, symbol: str, day: str) -> pd.DataFrame:
    return qdb.df(
        "SELECT ts_recv, price, size, side FROM mbo_events "
        f"WHERE action='T' AND symbol='{symbol}' "
        f"AND ts_recv >= '{day}T00:00:00.000000Z' "
        f"AND ts_recv <  '{day}T23:59:59.999999Z' ORDER BY ts_recv")


def replicate(df: pd.DataFrame) -> dict[tuple[int, int], tuple[float, float]]:
    """edge-per-contract and copied volume for every (size filter, horizon)."""
    if len(df) < 1000:
        return {}
    ts = pd.to_datetime(df["ts_recv"]).astype("int64").to_numpy() // 1_000_000_000
    px = df["price"].to_numpy(float)
    sz = df["size"].to_numpy(float)
    sgn = np.where(df["side"].to_numpy() == "B", 1.0, -1.0)   # B = buy aggressor

    t0 = ts[0]
    idx = ts - t0
    n = int(idx[-1]) + 1
    # last traded price for every second of the session (forward filled)
    last = np.full(n, np.nan)
    last[idx] = px                       # later writes win -> last price in second
    s = pd.Series(last).ffill().to_numpy()

    out = {}
    for S in SIZES:
        m = sz >= S
        if m.sum() < 50:
            continue
        vol = float(sz[m].sum())
        for H in HORIZONS:
            fut = np.minimum(idx[m] + H, n - 1)
            pnl = float((sgn[m] * sz[m] * (s[fut] - px[m])).sum())
            out[(S, H)] = (pnl / vol, vol)
    return out


def run(symbol: str, ndays: int, top: int, start: str | None,
        end: str | None) -> None:
    qdb = QuestDB(timeout=240.0)
    days = session_days(symbol, start, end)
    if not days:
        raise SystemExit(f"no candidate days for {symbol}")
    days = days[-ndays:] if ndays else days
    print(f"{symbol}: {len(days)} sessions ({days[0]}..{days[-1]})", flush=True)

    per_day: dict[tuple[int, int], list[float]] = {}
    best_per_day: list[tuple[str, int, int, float]] = []
    vols: dict[int, list[float]] = {}
    for i, d in enumerate(days):
        try:
            df = load_day(qdb, symbol, d)
        except Exception as ex:                      # noqa: BLE001
            print(f"  {d}: load failed ({str(ex)[:60]})", flush=True)
            continue
        if len(df) < 50_000:            # holiday / half session / no data
            continue
        r = replicate(df)
        if not r:
            continue
        for k, (edge, vol) in r.items():
            per_day.setdefault(k, []).append(edge)
            vols.setdefault(k[0], []).append(vol)
        bk = max(r, key=lambda k: r[k][0])
        best_per_day.append((d, bk[0], bk[1], r[bk][0]))
        if i % 5 == 0:
            print(f"  {d}: {len(df):,} trades, best S={bk[0]} H={bk[1]}s "
                  f"edge={r[bk][0]:+.4f}pt/contract", flush=True)

    if not per_day:
        raise SystemExit("no sessions produced results")
    nd = len(best_per_day)

    print("\n" + "=" * 92)
    print(f"TRADE REPLICATION — {symbol}, {nd} sessions")
    print("edge per contract, POINTS. Copying an aggressor buys the ask / sells")
    print("the bid, so the spread (0.25pt) is already inside these fills.")
    print("=" * 92)
    print("\nENTRY = size filter (rows).  EXIT = hold horizon (cols).  cell = median")
    print("edge/contract across sessions; (sign%) = share of sessions agreeing.\n")
    hdr = "".join(f"{f'{H}s':>15}" for H in HORIZONS)
    print(f"{'copy size>=':>12}{hdr}")
    for S in SIZES:
        cells = []
        for H in HORIZONS:
            v = np.array(per_day.get((S, H), []))
            if len(v) < 5:
                cells.append(f"{'-':>15}")
                continue
            med = float(np.median(v))
            hit = float(max((v > 0).mean(), (v < 0).mean()) * 100)
            cells.append(f"{med:+8.4f}({hit:3.0f}%)")
        vm = np.median(vols.get(S, [0]))
        print(f"{S:>9} lots" + "".join(cells) + f"   [{vm:,.0f} lots/day]")

    print("\n  A cell is only real if the sign holds across sessions. Read the H")
    print("  axis left-to-right: rising = permanent impact (informed flow you can")
    print("  own); peak-then-fade = transient (you are renting someone's move).")

    # ── the actual question: does the best (S,H) CLUSTER, and does it DRIFT? ──
    bs = pd.DataFrame(best_per_day, columns=["day", "S", "H", "edge"])
    print(f"\n[CLUSTERING] best (S,H) per session, {nd} sessions")
    cnt = bs.groupby(["S", "H"]).size().sort_values(ascending=False)
    for (S, H), c in cnt.head(top).items():
        print(f"    S={S:>3} lots  H={H:>4}s   chosen on {c:2d}/{nd} sessions "
              f"({100*c/nd:4.1f}%)")
    print(f"    distinct optima: {len(cnt)}  "
          f"-- concentrated = a real parameter, scattered = fitting noise")
    print(f"    S: median {bs.S.median():.0f}, IQR {bs.S.quantile(.25):.0f}"
          f"-{bs.S.quantile(.75):.0f}   |   "
          f"H: median {bs.H.median():.0f}s, IQR {bs.H.quantile(.25):.0f}"
          f"-{bs.H.quantile(.75):.0f}s")

    print(f"\n[DRIFT] per session, in order — is the optimum moving or jumping?")
    for d, S, H, e in best_per_day:
        print(f"    {d}  S={S:>3}  H={H:>4}s  edge={e:+.4f}pt")
    print("=" * 92 + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ESM5")
    ap.add_argument("--days", type=int, default=20, help="0 = all")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    a = ap.parse_args()
    run(a.symbol, a.days, a.top, a.start, a.end)


if __name__ == "__main__":
    main()
