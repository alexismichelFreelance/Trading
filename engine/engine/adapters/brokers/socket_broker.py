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
                 account: str = "Sim", symbol: str = "ES",
                 send_timeout: float = 5.0) -> None:
        self.host, self.port = host, port
        self.account = account
        self.symbol = symbol
        # An unbounded drain() is how a platform that stops READING stalls order
        # submission -- and submission happens inside the engine's dispatch loop,
        # so that stalls every strategy. Same defect class as the observer sinks
        # and the C# relay. Bounded: a stuck socket fails the order loudly instead
        # of freezing the engine.
        self.send_timeout = send_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._opened = 0          # connections opened; >1 means we reconnected

    def _dead(self) -> bool:
        """True if there is no usable connection. `is_closing()` is the part that
        was missing: _ensure() only ever checked `is None`, and nothing anywhere
        reset _writer, so once a socket died it was reused forever -- orders
        written into a closed pipe, no fills back, no error."""
        if self._writer is None or self._writer.is_closing():
            return True
        # A peer that closed is NOT visible on the write side: TCP accepts one
        # more write and only RSTs afterwards, so is_closing() stays False and the
        # order vanishes. EOF on the READ side is the honest signal, and the
        # engine's _pump_broker consumes events() continuously, so it is set.
        return self._reader is not None and self._reader.at_eof()

    async def _drop(self) -> None:
        """Tear down so the next _ensure() genuinely reconnects."""
        w, self._writer, self._reader = self._writer, None, None
        if w is not None:
            try:
                w.close()
            except Exception:                         # noqa: BLE001
                pass

    async def _ensure(self) -> None:
        async with self._lock:
            if not self._dead():
                return
            await self._drop()
            self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
            self._opened += 1
            log.info("%s broker connected %s:%d (%s)%s", self.name, self.host,
                     self.port, self.account,
                     f" [reconnect #{self.reconnects}]" if self.reconnects else "")

    @property
    def reconnects(self) -> int:
        """How many times this broker had to re-open its socket. Counted from
        connections opened rather than from _writer's state, because _drop() nulls
        _writer before _ensure() ever sees it."""
        return max(0, self._opened - 1)

    async def _send(self, obj: dict) -> None:
        """Deliver, or raise. Never silently drop: a dropped order the blotter
        thinks is live is worse than a failed one the caller can react to."""
        payload = encode(obj)
        for attempt in (1, 2):                        # one retry across a reconnect
            try:
                await self._ensure()
                assert self._writer is not None
                self._writer.write(payload)
                await asyncio.wait_for(self._writer.drain(), self.send_timeout)
                return
            except (OSError, asyncio.TimeoutError, AssertionError) as ex:
                await self._drop()
                if attempt == 2:
                    log.error("%s broker send FAILED after reconnect: %s: %s",
                              self.name, type(ex).__name__, ex)
                    raise
                log.warning("%s broker send failed (%s) -- reconnecting and retrying",
                            self.name, type(ex).__name__)

    async def submit(self, order: Order) -> None:
        await self._send(order_to_msg(order))

    async def cancel(self, order_id: str) -> None:
        await self._send(cancel_msg(order_id))

    async def modify(self, order_id: str, **changes) -> None:
        await self._send(modify_msg(order_id, **changes))

    async def events(self) -> AsyncIterator[BrokerEvent]:
        """Yields until the socket ends. On end the connection is DROPPED, which
        is what makes _pump_broker's reconnect real: it re-calls events(), and
        _ensure() then opens a genuinely new socket instead of handing back the
        dead one it had cached forever."""
        await self._ensure()
        reader = self._reader
        assert reader is not None
        try:
            async for raw in reader:
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
        finally:
            # The stream ended (or the consumer stopped): this socket is spent.
            # Dropping it here is what makes the caller's reconnect real -- it
            # re-enters events(), and _ensure() opens a NEW connection. Guarded so
            # a reconnect that already happened elsewhere is not torn down.
            if self._reader is reader:
                await self._drop()

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:                         # noqa: BLE001
                pass


__all__ = ["SocketBroker"]
