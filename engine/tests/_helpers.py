"""Plain importable test helpers (not a conftest, to avoid name clashes between
the two conftest.py files on sys.path)."""
from __future__ import annotations

from engine.core.orders import Order, OrderType


class FakeFeed:
    finite = True      # bounded fake: stream-end is DONE, not a disconnect
    """Async-yields a fixed list of pre-built events (strict ts order assumed)."""

    def __init__(self, events) -> None:
        self._events = list(events)

    async def stream(self):
        for e in self._events:
            yield e


class ScriptedStrategy:
    """Submits pre-scripted orders keyed by the (1-based) trade index it sees."""

    def __init__(self, symbol: str, script: dict[int, list[Order]]) -> None:
        self.symbol = symbol
        self.script = script
        self.i = 0
        self.fills = []

    def on_trade(self, e):
        self.i += 1
        return self.script.get(self.i, [])

    def on_quote(self, e):
        return []

    def on_depth(self, e):
        return []

    def on_bar(self, e):
        return []

    def on_bookflow(self, e):
        return []

    def on_fill(self, e):
        self.fills.append(e)

    def on_position(self, e):
        return None


def mkt(symbol, side, qty, tag=""):
    return Order(symbol=symbol, side=side, qty=qty, type=OrderType.MARKET, tag=tag)
