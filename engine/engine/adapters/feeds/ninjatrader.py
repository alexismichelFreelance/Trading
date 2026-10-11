"""NinjaTraderFeed — consumes NT8's (free, 40-min-delayed) feed via the
NinjaScript relay socket and yields normalized events.

The relay sends line-delimited JSON (see engine/adapters/protocol.py). Trades /
quotes / bars pass through; raw DOM ticks feed a BookFlowAggregator, and the
per-second BookFlow is emitted at each 1-second boundary — after that second's
trades and before the next second's events — matching the replay ordering the
strategies expect. All NT-specific concerns (socket, reconnect) stay here; the
core stays pure.

MULTI-SYMBOL, SINGLE SOCKET (demultiplex): one relay may stream several
instruments over one socket. Every wire message can carry its own ``symbol``
key (the EngineAddOn tags everything); the event's symbol then selects its own
BookFlow aggregator, its own per-second boundary and its own 1m bar builder.
Lanes auto-register on first event — no hardcoded roster — so the feed is
forward-compatible with any instrument the bridge streams. A legacy single-
instrument relay that sends no ``symbol`` key falls back to the configured
``symbol`` (behaviour unchanged), so existing single-lane deployments and the
loopback/multilane tests are untouched.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from ...core.events import Bar, DepthUpdate, MarketEvent, Trade
from ...features.bookflow import BookFlowAggregator
from ..protocol import decode_market

log = logging.getLogger("engine.nt.feed")
NS = 1_000_000_000


class _BarBuilder:
    """Per-symbol 1m bar accumulator (open/high/low/close/volume) built from
    trades, emitted at each minute rollover (close-stamped). One per lane."""

    __slots__ = ("minute", "o", "h", "l", "c", "v")

    def __init__(self) -> None:
        self.minute: int | None = None
        self.o = self.h = self.l = self.c = 0.0
        self.v = 0

    def start(self, ev: Trade) -> None:
        self.minute = ev.ts // (60 * NS)
        self.o = self.h = self.l = self.c = ev.price
        self.v = ev.size

    def absorb(self, ev: Trade) -> None:
        self.h = max(self.h, ev.price)
        self.l = min(self.l, ev.price)
        self.c = ev.price
        self.v += ev.size


class NinjaTraderFeed:
    def __init__(self, host: str = "127.0.0.1", port: int = 36001,
                 symbol: str = "ES", symbols: list[str] | None = None,
                 emit_depth: bool = False) -> None:
        self.host, self.port = host, port
        self.symbol = symbol
        self.symbols = list(symbols) if symbols else []
        self.emit_depth = emit_depth
        # The fallback symbol stamped onto a wire message that carries no
        # ``symbol`` key — i.e. a legacy single-instrument relay. With several
        # configured symbols the wire symbol is authoritative, so fall back to
        # "" (broadcast) rather than guessing which instrument owns the message.
        if len(self.symbols) == 1:
            self._default_sym = self.symbols[0]
        else:
            self._default_sym = symbol

    async def stream(self) -> AsyncIterator[MarketEvent]:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        log.info("NT feed connected %s:%d (demux symbols=%s)",
                 self.host, self.port, self.symbols or "auto")
        # Per-symbol state, keyed by the symbol each parsed event carries.
        aggs: dict[str, BookFlowAggregator] = {}
        bars: dict[str, _BarBuilder] = {}
        sec_hi: dict[str, int] = {}          # last second boundary emitted, per lane
        try:
            async for raw in reader:                 # StreamReader yields lines
                line = raw.strip()
                if not line:
                    continue
                try:
                    ev = decode_market(json.loads(line), self._default_sym)
                except (json.JSONDecodeError, KeyError, ValueError) as ex:
                    log.warning("bad market msg %r: %s", line[:80], ex)
                    continue
                if ev is None:
                    continue
                sym = ev.symbol
                agg = aggs.get(sym)
                if agg is None:                      # auto-register this lane
                    agg = aggs[sym] = BookFlowAggregator()
                    bars[sym] = _BarBuilder()
                    sec_hi[sym] = -1
                sec = ev.ts // NS
                if sec > sec_hi[sym]:                # close the previous second
                    if sec_hi[sym] >= 0:
                        yield agg.snapshot(sec_hi[sym], sym)
                    sec_hi[sym] = sec
                if isinstance(ev, Trade):
                    bs = bars[sym]
                    minute = ev.ts // (60 * NS)
                    if bs.minute is None:
                        bs.start(ev)
                    elif minute > bs.minute:         # emit closed 1m bar first
                        yield Bar((bs.minute + 1) * 60 * NS, "1m",
                                  bs.o, bs.h, bs.l, bs.c, bs.v, sym)
                        bs.start(ev)
                    else:
                        bs.absorb(ev)
                    yield ev
                elif isinstance(ev, DepthUpdate):
                    agg.accumulate(ev)
                    if self.emit_depth:
                        yield ev
                else:
                    yield ev                          # Quote / Bar / BookFlow
            for sym, agg in aggs.items():             # flush final seconds
                if sec_hi[sym] >= 0:
                    yield agg.snapshot(sec_hi[sym], sym)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:                         # noqa: BLE001
                pass


__all__ = ["NinjaTraderFeed"]
