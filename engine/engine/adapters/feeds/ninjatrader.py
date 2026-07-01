"""NinjaTraderFeed — Phase 2 STUB (delayed real-time feed via NT8).

Consumes NinjaTrader 8's free 40-min-delayed real-time feed and yields the
engine's normalized events. All NT-specific wire code stays inside this adapter;
the core stays pure Python.

INTEGRATION (see docs/PHASE2_BRIDGES.md): a small NinjaScript add-on subscribes
to Bars/MarketData/MarketDepth and pushes JSON lines over a local TCP socket
(127.0.0.1:<port>); this adapter reads that socket, maps each message to a
Trade / Quote / DepthUpdate / BookFlow, and yields them in ts order. The
per-second BookFlow is aggregated here from the raw DOM add/cancel deltas
(exactly as the research SQL did from mbo_events), so strategies see the same
inputs as in replay.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from ...core.events import MarketEvent


class NinjaTraderFeed:
    def __init__(self, host: str = "127.0.0.1", port: int = 36001,
                 symbol: str = "ES", depth_levels: int = 10) -> None:
        self.host, self.port = host, port
        self.symbol = symbol
        self.depth_levels = depth_levels

    async def stream(self) -> AsyncIterator[MarketEvent]:
        raise NotImplementedError(
            "Phase 2: connect to the NinjaScript relay socket, parse JSON market "
            "messages, aggregate per-second BookFlow from DOM deltas, and yield "
            "Trade/Quote/DepthUpdate/BookFlow in ts order. Handle reconnect/backfill. "
            "See docs/PHASE2_BRIDGES.md."
        )
        yield  # pragma: no cover - makes this an async generator


__all__ = ["NinjaTraderFeed"]
