"""SocketBroker — shared JSON-line socket broker for the platform bridges.

One bidirectional local socket per broker: the engine writes place/cancel/modify
orders; the platform add-on submits them to its SIM account and streams
fill/position/account messages back, which become Fill/PositionUpdate/
AccountUpdate. Platform bridges differ only in the connector on the
other end (and the default port), so both subclass this.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from ...core.events import BrokerEvent
from ...core.orders import Order
from ..protocol import cancel_msg, decode_broker, encode, modify_msg, order_to_msg

log = logging.getLogger("engine.socket_broker")


class SocketBroker:
    name = "socket"

    def __init__(self, host: str = "127.0.0.1", port: int = 36002,
                 account: str = "Sim", symbol: str = "ES") -> None:
        self.host, self.port = host, port
        self.account = account
        self.symbol = symbol
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> None:
        async with self._lock:
            if self._writer is None:
                self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
                log.info("%s broker connected %s:%d (%s)", self.name, self.host, self.port, self.account)

    async def _send(self, obj: dict) -> None:
        await self._ensure()
        assert self._writer is not None
        self._writer.write(encode(obj))
        await self._writer.drain()

    async def submit(self, order: Order) -> None:
        await self._send(order_to_msg(order))

    async def cancel(self, order_id: str) -> None:
        await self._send(cancel_msg(order_id))

    async def modify(self, order_id: str, **changes) -> None:
        await self._send(modify_msg(order_id, **changes))

    async def events(self) -> AsyncIterator[BrokerEvent]:
        await self._ensure()
        assert self._reader is not None
        async for raw in self._reader:
            line = raw.strip()
            if not line:
                continue
            try:
                be = decode_broker(json.loads(line))
            except (json.JSONDecodeError, KeyError, ValueError) as ex:
                log.warning("bad broker msg %r: %s", line[:80], ex)
                continue
            if be is not None:
                yield be

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:                         # noqa: BLE001
                pass


__all__ = ["SocketBroker"]
