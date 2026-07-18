"""Symbol-filtered dispatch: stamped events are isolated per strategy;
unstamped events broadcast (pre-multi-instrument behavior preserved)."""
from engine.core.dispatch import dispatch_broker, dispatch_market
from engine.core.events import BUY, Bar, Fill, Trade
from engine.strategies.base import BaseStrategy


class Probe(BaseStrategy):
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.trades: list[Trade] = []
        self.bars: list[Bar] = []
        self.fills: list[Fill] = []

    def on_trade(self, e):
        self.trades.append(e)
        return []

    def on_bar(self, e):
        self.bars.append(e)
        return []

    def on_fill(self, e):
        self.fills.append(e)


def test_stamped_events_isolated():
    es, nq = Probe("ES"), Probe("NQ")
    dispatch_market([es, nq], Trade(1, 6000.0, 1, BUY, "ES"))
    dispatch_market([es, nq], Trade(2, 20000.0, 2, BUY, "NQ"))
    dispatch_market([es, nq], Bar(3, "1m", 1, 2, 0, 1, 5, "NQ"))
    assert [t.price for t in es.trades] == [6000.0]
    assert [t.price for t in nq.trades] == [20000.0]
    assert len(es.bars) == 0 and len(nq.bars) == 1


def test_unstamped_events_broadcast():
    es, nq = Probe("ES"), Probe("NQ")
    dispatch_market([es, nq], Trade(1, 5000.0, 1, BUY))       # symbol=""
    assert len(es.trades) == 1 and len(nq.trades) == 1


def test_symbolless_strategy_sees_everything():
    watcher = Probe("")                                        # observer role
    dispatch_market([watcher], Trade(1, 6000.0, 1, BUY, "ES"))
    dispatch_market([watcher], Trade(2, 20000.0, 1, BUY, "NQ"))
    assert len(watcher.trades) == 2


def test_broker_events_filtered():
    es, nq = Probe("ES"), Probe("NQ")
    dispatch_broker([es, nq], Fill(1, "o1", "ES", 6000.0, 1, 0.0, 0.0))
    dispatch_broker([es, nq], Fill(2, "o2", "NQ", 20000.0, -1, 0.0, 0.0))
    assert [f.order_id for f in es.fills] == ["o1"]
    assert [f.order_id for f in nq.fills] == ["o2"]
