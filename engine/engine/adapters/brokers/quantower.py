"""QuantowerBroker — routes orders to a Quantower SIM account via the Quantower
connector socket, so fills/positions render natively in the Quantower UI. Same
JSON protocol as NinjaTrader (SocketBroker), different default port — this is how
the two front-ends are compared on the same engine + strategies."""
from __future__ import annotations

from .socket_broker import SocketBroker


class QuantowerBroker(SocketBroker):
    name = "quantower"

    def __init__(self, host: str = "127.0.0.1", port: int = 36003,
                 account: str = "Sim", symbol: str = "ES") -> None:
        super().__init__(host, port, account, symbol)


__all__ = ["QuantowerBroker"]
