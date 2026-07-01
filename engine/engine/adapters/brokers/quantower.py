"""QuantowerBroker — Phase 2 STUB. Routes the engine's orders to a Quantower SIM
account so fills/positions render natively in the Quantower UI (the second
front-end being evaluated against NinjaTrader).

INTEGRATION (see docs/PHASE2_BRIDGES.md): a small Quantower add-on (its .NET
API / algo plugin) exposes a local socket order channel mirroring the NT one;
this adapter speaks the same JSON order protocol, so only the connector class
differs — the core and strategies are unchanged.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from ...core.events import BrokerEvent
from ...core.orders import Order


class QuantowerBroker:
    def __init__(self, host: str = "127.0.0.1", port: int = 36003,
                 account: str = "Sim", symbol: str = "ES") -> None:
        self.host, self.port = host, port
        self.account = account
        self.symbol = symbol

    async def submit(self, order: Order) -> None:
        raise NotImplementedError("Phase 2: send order to Quantower connector (SIM account).")

    async def cancel(self, order_id: str) -> None:
        raise NotImplementedError("Phase 2: send cancel to Quantower connector.")

    async def modify(self, order_id: str, **changes) -> None:
        raise NotImplementedError("Phase 2: send modify to Quantower connector.")

    async def events(self) -> AsyncIterator[BrokerEvent]:
        raise NotImplementedError(
            "Phase 2: read trade/position/account updates from the connector and "
            "map to Fill/PositionUpdate/AccountUpdate.")
        yield  # pragma: no cover


__all__ = ["QuantowerBroker"]
