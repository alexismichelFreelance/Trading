"""How much traffic do we actually push at the NT8 draw socket?

2026-07-30 the user reported the chart running MINUTES behind on drawings and
manual trading going unresponsive. That is a throughput problem, not a mystery,
so measure it: drive the real PaintController with a real session's 1m bars
through a counting transport and count messages and bytes.

    .venv/Scripts/python.exe tools/paint_load.py [--day 2026-07-30] [--symbol ES]

Reports per-minute and peak-second load, and the breakdown by draw kind, so the
expensive part is identified rather than guessed at.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB          # noqa: E402
from engine.core.events import Bar                   # noqa: E402
from engine.painters import PaintController          # noqa: E402


class CountingPainter:
    """Same surface as NTChartPainter, but counts instead of sending."""

    def __init__(self) -> None:
        self.kinds = Counter()
        self.tags = Counter()
        self.bytes = 0
        self.msgs = 0
        self.per_bar: list[int] = []
        self.live_tags: set[str] = set()
        self.peak_objects = 0
        self.obj_curve: list[int] = []

    async def _send(self, obj: dict) -> None:
        obj["t"] = "draw"
        self.msgs += 1
        self.bytes += len(json.dumps(obj)) + 1
        self.kinds[obj.get("kind", "?")] += 1
        t = str(obj.get("tag", ""))
        self.tags[t] += 1
        if obj.get("kind") == "remove":
            self.live_tags.discard(t)
        elif t:
            self.live_tags.add(t)
        self.peak_objects = max(self.peak_objects, len(self.live_tags))

    async def arrow(self, tag, ts, price, direction, color="", label="") -> None:
        await self._send(dict(kind="arrow", tag=tag, ts=ts, price=price,
                              dir=direction, color=color, label=label))

    async def rect(self, tag, ts1, p1, ts2, p2, color="", opacity=0) -> None:
        await self._send(dict(kind="rect", tag=tag, ts=ts1, price=p1, ts2=ts2,
                              price2=p2, color=color, opacity=opacity))

    async def hline(self, tag, price, color="") -> None:
        await self._send(dict(kind="hline", tag=tag, price=price, color=color))

    async def line(self, tag, ts1, p1, ts2, p2, color="") -> None:
        await self._send(dict(kind="line", tag=tag, ts=ts1, price=p1, ts2=ts2,
                              price2=p2, color=color))

    async def text(self, tag, ts, price, txt, color="") -> None:
        await self._send(dict(kind="text", tag=tag, ts=ts, price=price,
                              text=txt, color=color))

    async def status(self, txt, pos="bottomleft") -> None:
        await self._send(dict(kind="status", text=txt, pos=pos))

    async def remove(self, tag) -> None:
        await self._send(dict(kind="remove", tag=tag))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-07-30")
    ap.add_argument("--symbol", default="ES")
    ap.add_argument("--warmup-days", type=int, default=15,
                    help="days of prior bars fed first, so the zone book is as "
                         "populated as it would be in a live session")
    a = ap.parse_args()
    q = QuestDB(timeout=120.0)
    start = (pd.Timestamp(a.day) - pd.Timedelta(days=a.warmup_days)).strftime("%Y-%m-%d")
    allb = q.df("SELECT ts,o,h,l,c,vol FROM claude_bars_live "
                f"WHERE symbol='{a.symbol}' AND ts>='{start}T00:00:00Z' "
                f"AND ts<'{a.day}T23:59:59Z' ORDER BY ts")
    if allb.empty:
        raise SystemExit(f"no bars for {a.symbol}")
    allb["day"] = pd.to_datetime(allb.ts, utc=True).dt.tz_convert(
        "America/New_York").dt.strftime("%Y-%m-%d")
    warm = allb[allb.day < a.day].reset_index(drop=True)
    br = allb[allb.day == a.day].reset_index(drop=True)
    if br.empty:
        raise SystemExit(f"no bars for {a.symbol} on {a.day}")
    ts = pd.to_datetime(br.ts, utc=True).astype("int64").to_numpy()
    cp = CountingPainter()
    pc = PaintController(cp, strategies=[])

    async def go():
        # warm the zone book exactly as a live session would arrive at it
        wts = pd.to_datetime(warm.ts, utc=True).astype("int64").to_numpy()
        for i in range(len(warm)):
            pc.zv.update(Bar(int(wts[i]), "1m", float(warm.o.iloc[i]),
                             float(warm.h.iloc[i]), float(warm.l.iloc[i]),
                             float(warm.c.iloc[i]), int(warm.vol.iloc[i]), a.symbol))
        z = sum(len(pc.zv.book[tf].zones) for tf in pc.zv.book)
        print(f"  (warmed with {len(warm):,} prior bars -> {z} zones in the book)")
        prev = 0
        for i in range(len(br)):
            b = Bar(int(ts[i]), "1m", float(br.o.iloc[i]), float(br.h.iloc[i]),
                    float(br.l.iloc[i]), float(br.c.iloc[i]),
                    int(br.vol.iloc[i]), a.symbol)
            await pc.on_bar(b, live=True, backfill_bars=0)
            cp.per_bar.append(cp.msgs - prev)
            cp.obj_curve.append(len(cp.live_tags))
            prev = cp.msgs

    asyncio.run(go())
    n = len(br)
    pb = pd.Series(cp.per_bar)
    print(f"\n{'='*76}")
    print(f"NT8 DRAW LOAD — {a.symbol} {a.day}, {n} one-minute bars, ONE lane")
    print(f"{'='*76}")
    print(f"  total draw messages : {cp.msgs:>10,}")
    print(f"  total bytes         : {cp.bytes:>10,}  ({cp.bytes/1e6:.1f} MB)")
    print(f"  per bar  mean/median: {pb.mean():>10.1f} / {pb.median():.0f}")
    print(f"  per bar  p95 / MAX  : {pb.quantile(.95):>10.0f} / {pb.max()}")
    print(f"\n  A 4-minute redraw cycle lands in ONE bar, so the max above is what")
    print(f"  NT8 must absorb in a single burst -- x2 for two lanes (ES + NQ).")
    oc = pd.Series(cp.obj_curve)
    print(f"\n  CHART OBJECTS NT8 must hold, update and hit-test every render:")
    print(f"    distinct tags ever drawn  : {len(cp.tags):>8,}")
    print(f"    live objects start -> end : {oc.iloc[0]:>8,} -> {oc.iloc[-1]:,}")
    print(f"    PEAK live objects         : {cp.peak_objects:>8,}")
    print(f"    redraws per object (mean) : {cp.msgs/max(len(cp.tags),1):>8.1f}")
    print(f"\n  by kind:")
    for k, v in cp.kinds.most_common():
        print(f"    {k:<10} {v:>9,}  ({100*v/cp.msgs:4.1f}%)")
    print(f"\n  top tag families:")
    for k, v in cp.tags.most_common(8):
        print(f"    {k:<34} {v:>9,}")
    print(f"\n  bursts (bars needing >200 messages): "
          f"{int((pb > 200).sum())} of {n}")
    print(f"{'='*76}\n")


if __name__ == "__main__":
    main()
