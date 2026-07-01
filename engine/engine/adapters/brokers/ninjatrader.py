"""NinjaTraderBroker — Phase 2 STUB. Routes the engine's orders to a NinjaTrader
8 SIM account so fills/positions render natively in the NT8 UI (one of the two
front-ends being evaluated).

INTEGRATION (see docs/PHASE2_BRIDGES.md): the same NinjaScript add-on that
relays the feed also exposes an order channel — the adapter sends
place/modify/cancel commands over the local socket; the add-on submits them to
the SIM account via NinjaScript's order API and relays Execution/Order/Position
updates back, which this adapter maps to Fill / PositionUpdate / AccountUpdate.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from ...core.events import BrokerEvent
from ...core.orders import Order


class NinjaTraderBroker:
    def __init__(self, host: str = "127.0.0.1", port: int = 36002,
                 account: str = "Sim101", symbol: str = "ES") -> None:
        self.host, self.port = host, port
        self.account = account
        self.symbol = symbol

    async def submit(self, order: Order) -> None:
        raise NotImplementedError("Phase 2: send order to NinjaScript relay (SIM account).")

    async def cancel(self, order_id: str) -> None:
        raise NotImplementedError("Phase 2: send cancel to NinjaScript relay.")

    async def modify(self, order_id: str, **changes) -> None:
        raise NotImplementedError("Phase 2: send modify to NinjaScript relay.")

    async def events(self) -> AsyncIterator[BrokerEvent]:
        raise NotImplementedError(
            "Phase 2: read Execution/Position/Account updates from the relay and "
            "map to Fill/PositionUpdate/AccountUpdate.")
        yield  # pragma: no cover


__all__ = ["NinjaTraderBroker"]
