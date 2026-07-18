"""NinjaTraderBroker — routes orders to a NinjaTrader 8 SIM account via the
NinjaScript relay socket, so fills/positions render natively in the NT8 UI.
Only the C# connector on the other end is platform-specific; the protocol is
shared (see SocketBroker + docs/PHASE2_BRIDGES.md)."""
from __future__ import annotations

from .socket_broker import SocketBroker


class NinjaTraderBroker(SocketBroker):
    name = "ninjatrader"

    def __init__(self, host: str = "127.0.0.1", port: int = 36002,
                 account: str = "Sim101", symbol: str = "ES") -> None:
        super().__init__(host, port, account, symbol)


__all__ = ["NinjaTraderBroker"]
