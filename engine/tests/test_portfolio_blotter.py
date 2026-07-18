"""Portfolio Blotter: per-symbol ledgers, avg-cost isolation, mixed point_usd."""
import logging

from engine.core.blotter import Blotter
from engine.core.events import Fill


def F(ts, oid, sym, px, size, comm=0.0):
    return Fill(ts, oid, sym, px, size, comm, 0.0)


def test_single_symbol_unchanged():
    b = Blotter("ESM5", 50.0)
    b.on_broker_event(F(1, "a", "ESM5", 6000.0, 1))
    b.on_broker_event(F(2, "b", "ESM5", 6004.0, -1))
    assert len(b.trades) == 1
    assert b.trades[0].gross_points == 4.0
    assert b.net_usd() == 200.0


def test_interleaved_symbols_isolated():
    b = Blotter("ES", 50.0)
    b.add_instrument("NQ", 20.0)
    # interleave: ES long scale-in while NQ short — avg-cost must not mix
    b.on_broker_event(F(1, "e1", "ES", 6000.0, 1))
    b.on_broker_event(F(2, "n1", "NQ", 20000.0, -2))
    b.on_broker_event(F(3, "e2", "ES", 6002.0, 1))      # ES avg 6001
    b.on_broker_event(F(4, "n2", "NQ", 19990.0, 2))     # NQ closed: +10 x2
    b.on_broker_event(F(5, "e3", "ES", 6006.0, -2))     # ES closed: +5 x2
    assert len(b.trades) == 2
    es, nq = b.trades[0], b.trades[1]                   # sorted by entry_ts
    assert es.symbol == "ES" and es.gross_points == 10.0      # (6006-6001)*2
    assert nq.symbol == "NQ" and nq.gross_points == 20.0      # (20000-19990)*2
    # dollars use each symbol's own multiplier
    assert b.net_usd() == 10.0 * 50.0 + 20.0 * 20.0


def test_flip_through_zero_per_symbol():
    b = Blotter("ES", 50.0)
    b.add_instrument("NQ", 20.0)
    b.on_broker_event(F(1, "e1", "ES", 6000.0, 1))
    b.on_broker_event(F(2, "e2", "ES", 6010.0, -2))     # flip: close +10, open short 1
    b.on_broker_event(F(3, "n1", "NQ", 20000.0, 1))     # NQ unaffected by ES flip
    b.on_broker_event(F(4, "e3", "ES", 6005.0, 1))      # close short: +5
    assert len(b.trades) == 2                            # NQ still open
    assert sum(t.gross_points for t in b.trades) == 15.0
    assert b.ledger_for("NQ").qty == 1


def test_unregistered_symbol_warns_and_isolates(caplog):
    b = Blotter("ES", 50.0)
    with caplog.at_level(logging.WARNING, logger="engine.blotter"):
        b.on_broker_event(F(1, "g1", "GC", 3300.0, 1))
    assert any("UNREGISTERED" in r.message for r in caplog.records)
    b.on_broker_event(F(2, "g2", "GC", 3310.0, -1))
    assert len(b.trades) == 1 and b.trades[0].symbol == "GC"
    # ES ledger untouched
    assert b.ledger.qty == 0
