"""Portfolio replay — the whole roster against one tape, with the peer channel on.

Every study so far ran ONE sleeve in isolation. That cannot see the two things
that actually decide portfolio behaviour:

  * CONCENTRATION. On 2026-07-27 the engine made +$16,852 and ~all of it was a
    single directional idea expressed nine ways -- ES onbreak, onbreak_gex,
    opendrive and opendrive_gex returned IDENTICALLY +26.25pt. That is not nine
    sleeves agreeing, it is one trade counted nine times, and no single-sleeve
    backtest can show it.
  * PEER EXITS. tools/exit_thesis_lab.py could measure the thesis (T) and
    invalidation (I) exits but NOT the peer exit (O), because O needs other
    sleeves running against the same tape. This provides that.

It reuses LiveEngine itself rather than ReplayEngine. ReplayEngine broadcasts
fills to every strategy (dispatch_broker(self.strategies, be)) -- correct for
single-sleeve parity, wrong for a roster, where each sleeve must own its book.
LiveEngine already has per-strategy ownership, the paper-fill path and the
Signal channel, so driving it from a historical feed means the replay IS the
live code path, not a reimplementation of it.

Trades come from mbo_events (REAL ticks). That matters: sweepfade clusters the
tape at 1ms, so the per-second aggregates in claude_sec_feat cannot drive it.
Bars come from claude_bars_1m for the bar-driven sleeves.

    .venv/Scripts/python.exe tools/portfolio_replay.py --symbol ESM5 --days 5
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB                      # noqa: E402
from engine.core.blotter import Blotter                          # noqa: E402
from engine.core.clock import EventClock                         # noqa: E402
from engine.core.events import BUY, SELL, Bar, Trade             # noqa: E402
from engine.core.live_engine import LiveEngine                   # noqa: E402
from tools.flow_replication import session_days                  # noqa: E402
from tools.run_live import ALL_LABELS, _make                     # noqa: E402

_MIN_NS = 60 * 1_000_000_000
POINT_USD = 50.0        # ES; NQ lanes would pass their own

# Reloading ~540k mbo_events rows per run took 257s and is what put QuestDB down
# on 2026-07-28. Each session is fetched ONCE and parked on disk after that.
CACHE_DIR = ROOT / ".cache" / "replay"


class HistFeed:
    finite = True      # bounded: stream-end means DONE, not a disconnect
    """Historical events for one session, in ts order, as a live-shaped feed."""

    def __init__(self, events):
        self._events = events

    async def stream(self):
        for e in self._events:
            yield e


class NullBroker:
    """All sleeves are PAPER here, so no order ever reaches a broker. It only
    has to exist and end its event stream cleanly."""

    finite = True      # bounded: stream-end is DONE, not a disconnect

    async def events(self):
        return
        yield           # pragma: no cover - makes this an async generator

    async def submit(self, o):        # pragma: no cover - never called on paper
        raise AssertionError("portfolio replay is paper-only; nothing routes live")


def load_events(qdb: QuestDB, symbol: str, day: str) -> list:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cf = CACHE_DIR / f"{symbol}_{day}.npz"
    if cf.exists():
        z = np.load(cf)
        return _events_from(pd.DataFrame({k: z[k] for k in z.files}), symbol)
    df = _fetch(qdb, symbol, day)
    if df is not None and len(df):
        np.savez_compressed(cf, **{c: df[c].to_numpy() for c in df.columns})
        return _events_from(df, symbol)
    return []


def _events_from(df: pd.DataFrame, symbol: str) -> list:
    """Build the event objects from NUMPY arrays, not per-row .iloc.

    This is where the time actually went: 540k rows x 6 fields of `.iloc[i]`
    cost ~290s, which is why adding a disk cache barely helped (327s -> 290s).
    The DB was never the bottleneck; pandas scalar indexing was. Pull each
    column out once and index the arrays.
    """
    ts = df["ts"].to_numpy(dtype="int64")
    kind = df["kind"].to_numpy(dtype="int8")
    a = df["a"].to_numpy(dtype=float)
    b = df["b"].to_numpy(dtype=float)
    c = df["c"].to_numpy(dtype=float)
    d = df["d"].to_numpy(dtype=float)
    e = df["e"].to_numpy(dtype="int64")
    order = np.lexsort((kind, ts))            # ts asc, bars (kind 0) before trades
    out = []
    for i in order:
        t = int(ts[i])
        if kind[i] == 1:
            out.append(Trade(t, float(a[i]), int(b[i]), int(c[i]), symbol))
        else:
            out.append(Bar(t, "1m", float(a[i]), float(b[i]), float(c[i]),
                           float(d[i]), int(e[i]), symbol))
    return out


def _fetch(qdb: QuestDB, symbol: str, day: str):
    """Real ticks + 1m bars, merged. Bars are emitted at their CLOSE and ordered
    BEFORE same-instant trades, exactly as ReplayFeed does, so coarse features
    update before the fine ones."""
    """Real ticks + 1m bars for one session, flattened for the parquet cache."""
    tr = qdb.df("SELECT ts_recv, price, size, side FROM mbo_events "
                f"WHERE action='T' AND symbol='{symbol}' "
                f"AND ts_recv >= '{day}T00:00:00.000000Z' "
                f"AND ts_recv <  '{day}T23:59:59.999999Z' ORDER BY ts_recv")
    if tr.empty:
        return None
    rows = pd.DataFrame({
        "ts": pd.to_datetime(tr["ts_recv"]).astype("int64"),
        "kind": 1,
        "a": tr["price"].astype(float),
        "b": tr["size"].astype(float),
        "c": np.where(tr["side"].to_numpy() == "B", BUY, SELL),
        "d": 0.0, "e": 0})
    br = qdb.df("SELECT ts,o,h,l,c,vol FROM claude_bars_1m "
                f"WHERE symbol='{symbol}' "
                f"AND ts >= '{day}T00:00:00.000000Z' "
                f"AND ts <  '{day}T23:59:59.999999Z' ORDER BY ts")
    if len(br):
        rows = pd.concat([rows, pd.DataFrame({
            "ts": pd.to_datetime(br["ts"]).astype("int64") + _MIN_NS,
            "kind": 0, "a": br["o"].astype(float), "b": br["h"].astype(float),
            "c": br["l"].astype(float), "d": br["c"].astype(float),
            "e": br["vol"].astype(int)})], ignore_index=True)
    return rows.sort_values("ts").reset_index(drop=True)


def pnl_of(fills, point_usd: float) -> float:
    pos, cost, real = 0, 0.0, 0.0
    for f in fills:
        qd = int(f.size)
        while qd:
            if pos and (pos > 0) != (qd > 0):
                n = min(abs(qd), abs(pos))
                sgn = 1 if pos > 0 else -1
                real += n * (f.price - cost) * sgn
                pos -= n * sgn
                qd -= n * (1 if qd > 0 else -1)
            else:
                tot = abs(pos) + abs(qd)
                cost = (cost * abs(pos) + f.price * abs(qd)) / tot
                pos += qd
                qd = 0
    return real * point_usd


async def run_day(qdb, symbol, day, labels, peer_map):
    ev = load_events(qdb, symbol, day)
    if len(ev) < 10_000:
        return None
    strats = []
    for lb in labels:
        s = _make(lb, symbol=symbol)
        s.label = f"{symbol}:{lb}"
        if lb in peer_map and hasattr(s, "peer_exit"):
            s.peer_exit = tuple(f"{symbol}:{p}" for p in peer_map[lb])
        strats.append(s)
    eng = LiveEngine(HistFeed(ev), NullBroker(), strats, EventClock(),
                     Blotter(symbol, POINT_USD), warmup_gate=False,
                     live_owners=set())        # set() = every sleeve is PAPER
    await eng.run()
    by = {}
    for f in eng.paper_fills:
        s = eng._owner.get(f.order_id)
        by.setdefault(eng.label_of(s), []).append(f)
    flat = []
    for f in eng.paper_fills:
        st = eng._owner.get(f.order_id)
        flat.append({"ts": f.ts, "symbol": f.symbol, "sleeve": eng.label_of(st),
                     "side": 1 if f.size > 0 else -1, "qty": abs(int(f.size)),
                     "price": f.price, "tag": f.tag})
    return {"day": day, "fills": by, "flat": flat, "signals": len(eng.signals),
            "peer_exits": eng._peer_exits, "n_events": len(ev)}


def report(rows, symbol, point_usd, labels):
    print("\n" + "=" * 84)
    print(f"PORTFOLIO REPLAY — {symbol}, {len(rows)} sessions, "
          f"{len(labels)} sleeves, peer channel ON")
    print("=" * 84)
    tot_sig = sum(r["signals"] for r in rows)
    tot_px = sum(r["peer_exits"] for r in rows)
    print(f"\nsignals broadcast {tot_sig:,}   peer-triggered exits {tot_px}")

    daily: dict[str, dict[str, float]] = {}
    for r in rows:
        for lb, fl in r["fills"].items():
            daily.setdefault(lb, {})[r["day"]] = pnl_of(fl, point_usd)
    if not daily:
        print("no sleeve produced fills")
        return
    df = pd.DataFrame(daily).fillna(0.0)

    print(f"\n{'sleeve':>22} {'days':>5} {'total $':>10} {'mean $':>9} {'sign%':>7}")
    for lb in df.columns[np.argsort(-df.sum().to_numpy())]:
        v = df[lb].to_numpy()
        nz = v[v != 0]
        sign = 100 * max((nz > 0).mean(), (nz < 0).mean()) if len(nz) else 0
        print(f"{lb:>22} {len(nz):5d} {v.sum():10,.0f} {v.mean():9,.0f} {sign:6.1f}%")
    print(f"{'PORTFOLIO':>22} {len(df):5d} {df.to_numpy().sum():10,.0f}")

    # ── concentration: is this a portfolio or one trade wearing many hats? ──
    print("\n[CONCENTRATION] pairwise daily-P&L correlation, |rho| >= 0.9")
    c = df.corr().fillna(0.0)
    dup = []
    for i, a in enumerate(c.columns):
        for b in c.columns[i + 1:]:
            if abs(c.loc[a, b]) >= 0.9:
                dup.append((a, b, c.loc[a, b]))
    for a, b, r in sorted(dup, key=lambda x: -abs(x[2]))[:15]:
        print(f"    {r:+.3f}  {a} ~ {b}")
    if not dup:
        print("    none -- sleeves are genuinely independent")
    tot = df.to_numpy().sum()
    top = df.sum().sort_values(ascending=False)
    if abs(tot) > 1e-9 and len(top):
        print(f"\n    top sleeve = {100*top.iloc[0]/tot:5.1f}% of portfolio P&L")
        print(f"    top 3      = {100*top.head(3).sum()/tot:5.1f}%")
    print("=" * 84 + "\n")


async def main_async(symbol, ndays, peer_map):
    qdb = QuestDB(timeout=240.0)
    labels = [lb for lb in ALL_LABELS if lb not in ("flow_fixed",)]
    rows, allflat = [], []
    for d in session_days(symbol, None, None)[-ndays:]:
        try:
            r = await run_day(qdb, symbol, d, labels, peer_map)
        except Exception as ex:                       # noqa: BLE001
            print(f"  {d}: FAILED {type(ex).__name__}: {str(ex)[:70]}", flush=True)
            continue
        if r:
            rows.append(r)
            allflat.extend(r["flat"])
            print(f"  {d}: {r['n_events']:,} events, "
                  f"{sum(len(v) for v in r['fills'].values())} fills, "
                  f"{r['peer_exits']} peer exits", flush=True)
    if not rows:
        raise SystemExit("no sessions replayed")
    if allflat:
        out = ROOT / ".cache" / f"replay_fills_{symbol}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(allflat).to_csv(out, index=False)
        print(f"\n  wrote {len(allflat)} replay fills -> {out}")
    report(rows, symbol, POINT_USD, labels)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ESM5")
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--peer-exit", default="",
                    help="sleeve:peer1|peer2,... e.g. sweepfade:onbreak|opendrive")
    a = ap.parse_args()
    pm = {}
    for part in filter(None, a.peer_exit.split(",")):
        k, _, v = part.partition(":")
        pm[k] = [x for x in v.split("|") if x]
    asyncio.run(main_async(a.symbol, a.days, pm))


if __name__ == "__main__":
    main()
