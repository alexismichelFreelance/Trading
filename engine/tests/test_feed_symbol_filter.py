"""Per-lane feeds must not ingest another lane's symbol.

The chartless EngineAddOn broadcasts EVERY subscribed symbol to EVERY
connected market client (one RelayChannel, all clients). Running ES and NQ as
two per-lane feeds on the shared port therefore delivered both symbols to both
feeds: duplicate bars, double-counted BookFlow, and two copies of every
strategy decision. The feed drops foreign symbols in per-lane mode; demux mode
(`symbols=[...]`) still owns several symbols on purpose.
"""
import asyncio
import json

from engine.adapters.feeds.ninjatrader import NinjaTraderFeed
from engine.core.events import Trade

NS = 1_000_000_000


def _trade(sym: str, sec: int, px: float) -> str:
    return json.dumps({"t": "trade", "ts": sec * NS, "symbol": sym,
                       "price": px, "size": 1, "aggressor": 1})


async def _collect(symbols, lines):
    async def handle(reader, writer):
        try:
            for line in lines:
                writer.write((line + "\n").encode())
            await writer.drain()
            await asyncio.sleep(0.3)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:                     # noqa: BLE001
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    feed = NinjaTraderFeed("127.0.0.1", port, symbol="ES", symbols=symbols)
    try:
        return [ev async for ev in feed.stream()]
    finally:
        server.close()
        await server.wait_closed()


def test_per_lane_feed_drops_foreign_symbols():
    events = asyncio.run(_collect(None, [
        _trade("ES", 1, 100.0),
        _trade("NQ", 2, 200.0),
        _trade("ES", 3, 101.0),
    ]))
    assert {ev.symbol for ev in events} == {"ES"}
    trades = [ev for ev in events if isinstance(ev, Trade)]
    assert [t.price for t in trades] == [100.0, 101.0]


def test_demux_feed_keeps_every_symbol():
    events = asyncio.run(_collect(["ES", "NQ"], [
        _trade("ES", 1, 100.0),
        _trade("NQ", 2, 200.0),
    ]))
    assert {ev.symbol for ev in events} == {"ES", "NQ"}
