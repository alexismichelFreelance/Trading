"""NinjaTraderFeed — consumes NT8's (free, 40-min-delayed) feed via the
NinjaScript relay socket and yields normalized events.

The relay sends line-delimited JSON (see engine/adapters/protocol.py). Trades /
quotes / bars pass through; raw DOM ticks feed a BookFlowAggregator, and the
per-second BookFlow is emitted at each 1-second boundary — after that second's
trades and before the next second's events — matching the replay ordering the
strategies expect. All NT-specific concerns (socket, reconnect) stay here; the
core stays pure.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from ...core.events import Bar, DepthUpdate, MarketEvent, Quote, Trade
from ...features.bookflow import BookFlowAggregator
from ..protocol import decode_market

log = logging.getLogger("engine.nt.feed")
NS = 1_000_000_000


class NinjaTraderFeed:
    def __init__(self, host: str = "127.0.0.1", port: int = 36001,
                 symbol: str = "ES", emit_depth: bool = False) -> None:
        self.host, self.port = host, port
        self.symbol = symbol
        self.emit_depth = emit_depth

    async def stream(self) -> AsyncIterator[MarketEvent]:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        log.info("NT feed connected %s:%d", self.host, self.port)
        agg = BookFlowAggregator()
        cur_sec: int | None = None
        try:
            async for raw in reader:                 # StreamReader yields lines
                line = raw.strip()
                if not line:
                    continue
                try:
                    ev = decode_market(json.loads(line))
                except (json.JSONDecodeError, KeyError, ValueError) as ex:
                    log.warning("bad market msg %r: %s", line[:80], ex)
                    continue
                if ev is None:
                    continue
                sec = ev.ts // NS
                if cur_sec is None:
                    cur_sec = sec
                elif sec > cur_sec:                  # close the previous second
                    yield agg.snapshot(cur_sec)
                    cur_sec = sec
                if isinstance(ev, DepthUpdate):
                    agg.accumulate(ev)
                    if self.emit_depth:
                        yield ev
                else:
                    yield ev                          # Trade / Quote / Bar / BookFlow
            if cur_sec is not None:                   # flush the final second
                yield agg.snapshot(cur_sec)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:                         # noqa: BLE001
                pass


__all__ = ["NinjaTraderFeed"]
