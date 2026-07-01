from dataclasses import FrozenInstanceError

import pytest

from engine.core.events import BUY, SELL, BookFlow, Fill, Trade


def test_trade_is_frozen():
    t = Trade(ts=1, price=5000.0, size=3, aggressor=BUY)
    assert t.aggressor == 1 and t.size == 3
    with pytest.raises(FrozenInstanceError):
        t.price = 5001.0  # type: ignore[misc]


def test_side_constants():
    assert (BUY, SELL) == (1, -1)


def test_bookflow_fields():
    bf = BookFlow(ts=10, bid_cancel=100, ask_cancel=50, bid_add=10, ask_add=5)
    assert bf.ask_cancel == 50 and bf.bid_add == 10


def test_fill_signed_size():
    f = Fill(ts=1, order_id="O1", symbol="ESM5", price=5000.0, size=-2,
             commission=0.0, slippage=0.0)
    assert f.size == -2  # short
