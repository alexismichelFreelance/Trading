"""Turning RAW depth capture off must not blind the BookFlow sleeves.

NQ raw depth was 11.5M of 15.5M daily rows into a 272M-row table nothing reads,
and it took QuestDB down mid-session on 2026-09-01. Switching it off is only
safe because NinjaTraderFeed calls agg.accumulate(ev) UNCONDITIONALLY and gates
only the raw DepthUpdate -- flow, ignition and open_drive read the per-second
BookFlow, not the depth. That distinction is one line deep in the feed, so it is
pinned here: get it wrong and three sleeves go quietly blind on a live lane.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.feeds.ninjatrader import NinjaTraderFeed   # noqa: E402
from engine.core.events import BookFlow, DepthUpdate, Trade     # noqa: E402

NS = 1_000_000_000
MSGS = [
    {"t": "depth", "ts": 1 * NS, "side": 1, "price": 5000.00, "size": 40, "level": 0},
    {"t": "depth", "ts": 1 * NS, "side": -1, "price": 5000.25, "size": 10, "level": 0},
    {"t": "trade", "ts": 1 * NS, "price": 5000.25, "size": 3, "aggressor": 1},
    {"t": "depth", "ts": 1 * NS, "side": 1, "price": 5000.00, "size": 90, "level": 0},
    {"t": "trade", "ts": 3 * NS, "price": 5000.50, "size": 1, "aggressor": 1},
]


async def _collect(emit_depth: bool):
    async def handle(reader, writer):
        for m in MSGS:
            writer.write((json.dumps(m) + "\n").encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    feed = NinjaTraderFeed("127.0.0.1", port, symbol="NQ", emit_depth=emit_depth)
    out = []
    async with server:
        async for ev in feed.stream():
            out.append(ev)
    return out


def run(emit_depth):
    return asyncio.run(_collect(emit_depth))


def test_depth_off_still_produces_bookflow():
    evs = run(emit_depth=False)
    assert not any(isinstance(e, DepthUpdate) for e in evs), "raw depth leaked"
    bf = [e for e in evs if isinstance(e, BookFlow)]
    assert bf, "BookFlow disappeared -- flow/ignition/open_drive would go blind"
    assert sum(b.bid_add for b in bf) > 0, "adds were not accumulated"


def test_depth_off_still_forwards_trades():
    evs = run(emit_depth=False)
    assert len([e for e in evs if isinstance(e, Trade)]) == 2


def test_depth_on_still_emits_raw_depth():
    evs = run(emit_depth=True)
    assert any(isinstance(e, DepthUpdate) for e in evs)
    assert any(isinstance(e, BookFlow) for e in evs)
