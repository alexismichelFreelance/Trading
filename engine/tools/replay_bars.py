"""replay_bars.py — run a BAR-DRIVEN sleeve over claude_bars_live history.

    .venv/Scripts/python.exe tools/replay_bars.py --strategy onfade --symbol ES
    .venv/Scripts/python.exe tools/replay_bars.py --strategy onfade --split 0.6

WHY THIS EXISTS instead of tools/run_replay.py. run_replay drives the engine
from the per-second flow table (claude_sec_feat*), which is the right feed for a
tick sleeve and the wrong one here twice over:

  1. COVERAGE. The sec tables start when the recorder started -- 56 sessions.
     claude_bars_live now holds 1,564 ES sessions after the .ncd import, and a
     sleeve that takes ONE trade per session needs the long table or there is
     nothing to measure.
  2. COST. tools/portfolio_replay.py took 132 minutes for 56 sessions, because
     a session is ~540k second-level events. A bar sleeve consumes 1,440 events
     a day and ignores every other kind.

This is NOT a second engine. It builds the real ReplayEngine, the real
SimBroker, the real Blotter and the real strategy object -- only the FEED is
different, so what it measures is the shipped sleeve and not a reimplementation
of it.

TIMESTAMPS. claude_bars_live is stamped at the bar CLOSE (see the memory note on
bar timestamp conventions and tools/import_ncd_bars.py, which shifts .ncd's
bar-OPEN stamps by +1 minute to match). ReplayFeed adds 60s to claude_bars_1m
because THAT table is stamped at the bar OPEN. Adding it here too would push
every bar a minute into the future and silently hand the sleeve the 09:31 bar as
its 09:30 open, so this feed emits `ts` unchanged.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np                                              # noqa: E402
import pandas as pd                                             # noqa: E402

from engine.adapters.brokers.sim import CleanFill, SimBroker    # noqa: E402
from engine.adapters.questdb import QuestDB                     # noqa: E402
from engine.core.blotter import Blotter                         # noqa: E402
from engine.core.clock import EventClock                        # noqa: E402
from engine.core.config import load_instruments                 # noqa: E402
from engine.core.engine import ReplayEngine                     # noqa: E402
from engine.core.events import Bar                              # noqa: E402

INSTRUMENTS = load_instruments(ROOT / "config" / "instruments.yaml")


class BarsFeed:
    """1-minute Bars from claude_bars_live, in ts order, emitted at their own
    timestamp. One query, held in memory: 1.7M bars is ~100MB and the
    alternative is 1,500 round trips."""

    def __init__(self, symbol: str, table: str = "claude_bars_live",
                 start: str | None = None, end: str | None = None,
                 timeout: int = 1800) -> None:
        self.symbol, self.table = symbol, table
        self.start, self.end = start, end
        self.timeout = timeout
        self.n = 0

    def _load(self) -> pd.DataFrame:
        w = [f"symbol = '{self.symbol}'"]
        if self.start:
            w.append(f"ts >= '{self.start}T00:00:00.000000Z'")
        if self.end:
            w.append(f"ts < '{self.end}T00:00:00.000000Z'")
        qdb = QuestDB(timeout=self.timeout)
        sql = (f"SELECT ts, o, h, l, c, vol FROM {self.table} "
               f"WHERE {' AND '.join(w)} ORDER BY ts")
        last = None
        for k in range(4):                      # QuestDB 400s on big queries
            try:
                return qdb.df(sql)
            except Exception as exc:            # noqa: BLE001
                last = exc
                time.sleep(10)
        raise SystemExit(f"bars query failed after 4 tries: {last}")

    async def stream(self) -> AsyncIterator[Bar]:
        df = self._load()
        ts = pd.to_datetime(df["ts"], utc=True).astype("int64").to_numpy()
        o = pd.to_numeric(df["o"]).to_numpy(float)
        h = pd.to_numeric(df["h"]).to_numpy(float)
        lo = pd.to_numeric(df["l"]).to_numpy(float)
        c = pd.to_numeric(df["c"]).to_numpy(float)
        v = pd.to_numeric(df["vol"]).fillna(0).to_numpy()
        sym = self.symbol
        self.n = len(df)
        for i in range(len(df)):
            yield Bar(int(ts[i]), "1m", o[i], h[i], lo[i], c[i], int(v[i]), sym)


def make_strategy(name: str, symbol: str, **kw):
    if name == "onfade":
        from engine.strategies.overnight_fade import OvernightFadeStrategy
        return OvernightFadeStrategy(symbol, **kw)
    if name == "onbreak":
        from engine.strategies.overnight_break import OvernightBreakStrategy
        return OvernightBreakStrategy(symbol, **kw)
    raise SystemExit(f"unknown bar strategy {name!r} (onfade/onbreak)")


async def run(name: str, symbol: str, start=None, end=None, **kw) -> Blotter:
    feed = BarsFeed(symbol, start=start, end=end)
    clk = EventClock()
    broker = SimBroker(symbol, clk, CleanFill())
    pusd = INSTRUMENTS[symbol].point_usd if symbol in INSTRUMENTS else 50.0
    blot = Blotter(symbol, pusd)
    t0 = time.time()
    await ReplayEngine(feed, broker, [make_strategy(name, symbol, **kw)],
                       clk, blot).run()
    print(f"  {feed.n:,} bars replayed in {time.time() - t0:,.0f}s")
    return blot


def report(blot: Blotter, cost_pts: float, split: float, point_usd: float) -> None:
    tr = blot.trades
    if not tr:
        print("\n  NO TRADES — the sleeve never fired.")
        return
    d = pd.DataFrame([{
        "day": pd.Timestamp(t.entry_ts, unit="ns", tz="UTC")
               .tz_convert("America/New_York").strftime("%Y-%m-%d"),
        "dir": t.direction,
        "pts": t.gross_points - cost_pts,
        "tag": (t.tags or ["?"])[-1],
    } for t in tr])
    d["usd"] = d.pts * point_usd

    def block(lab, x):
        if len(x) < 2:
            print(f"  {lab:>10}  n={len(x)}")
            return
        t = x.usd.mean() / (x.usd.std(ddof=1) / np.sqrt(len(x)))
        eq = x.usd.cumsum().to_numpy()
        dd = float((eq - np.maximum.accumulate(eq)).min())
        print(f"  {lab:>10}{len(x):>7}{x.usd.mean():>10,.0f}{t:>7.2f}"
              f"{(x.usd > 0).mean():>7.0%}{x.usd.sum():>12,.0f}{dd:>11,.0f}"
              f"{x.usd.sum() / abs(dd) if dd < 0 else float('nan'):>8.1f}")

    print(f"\n  {'':>10}{'n':>7}{'$/trade':>10}{'t':>7}{'win':>7}"
          f"{'total $':>12}{'maxDD':>11}{'ret/DD':>8}")
    cut = int(len(d) * split)
    block("TRAIN", d.iloc[:cut])
    block("TEST", d.iloc[cut:])
    block("ALL", d)
    print("\n  exits:", ", ".join(
        f"{k} {v} ({d[d.tag == k].usd.mean():,.0f}$)"
        for k, v in d.tag.value_counts().items()))
    print(f"  sides: long {int((d.dir > 0).sum())} / short {int((d.dir < 0).sum())}")
    yr = d.assign(y=d.day.str[:4]).groupby("y").usd.agg(["count", "mean", "sum"])
    print("\n  by year")
    for y, r in yr.iterrows():
        print(f"    {y}{int(r['count']):>6}{r['mean']:>10,.0f}{r['sum']:>12,.0f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="onfade")
    ap.add_argument("--symbol", default="ES")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--split", type=float, default=0.6,
                    help="train fraction, for the train/test rows")
    ap.add_argument("--cost", type=float, default=0.32,
                    help="round-turn cost in POINTS (0.32 ES pts = $16)")
    ap.add_argument("--tgt-frac", type=float)
    ap.add_argument("--stop-frac", type=float)
    a = ap.parse_args()
    kw = {}
    if a.tgt_frac is not None:
        kw["tgt_frac"] = a.tgt_frac
    if a.stop_frac is not None:
        kw["stop_frac"] = a.stop_frac
    print(f"REPLAY {a.strategy} on {a.symbol} from claude_bars_live"
          f"{'  ' + str(kw) if kw else ''}")
    blot = asyncio.run(run(a.strategy, a.symbol, a.start, a.end, **kw))
    pusd = INSTRUMENTS[a.symbol].point_usd if a.symbol in INSTRUMENTS else 50.0
    report(blot, a.cost, a.split, pusd)


if __name__ == "__main__":
    main()
