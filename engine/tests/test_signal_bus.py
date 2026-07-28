"""Peer signal channel: every strategy's INTENT is visible to the others, so a
sleeve can exit on information no single sleeve has.

The safety properties matter more than the happy path here. Broadcasting
POSITION state across sleeves is what caused the 2026-07-09 duplicate-flatten
runaway, so these tests pin that a Signal carries intent only, that an emitter
never hears itself, and that a peer's reply cannot cascade.
"""
import pandas as pd

from engine.core.dispatch import dispatch_signal
from engine.core.events import Signal, Trade
from engine.strategies.base import BaseStrategy
from engine.strategies.sweep_follow import SweepFollowStrategy

MS = 1_000_000


def _ts(s):
    return int(pd.Timestamp(s, tz="America/New_York").value)


class _Recorder(BaseStrategy):
    def __init__(self, symbol="ES"):
        self.symbol = symbol
        self.seen: list[Signal] = []

    def on_signal(self, e):
        self.seen.append(e)
        return []


class _Echo(BaseStrategy):
    """Replies to every signal — used to prove replies are not re-broadcast."""
    def __init__(self, symbol="ES"):
        self.symbol = symbol
        self.calls = 0

    def on_signal(self, e):
        from engine.core.orders import Order
        self.calls += 1
        return [Order(self.symbol, 1, 1, tag="echo")]


def test_default_strategy_ignores_peers():
    """All 23 existing sleeves inherit this: no behaviour change, parity safe."""
    assert BaseStrategy().on_signal(Signal(1, "ES", "x", 1, 1, "t", 100.0)) == []


def test_emitter_never_hears_itself():
    a, b = _Recorder(), _Recorder()
    sig = Signal(1, "ES", "ES:a", 1, 1, "entry", 100.0)
    dispatch_signal([a, b], sig, emitter=a)
    assert a.seen == [] and len(b.seen) == 1


def test_signal_does_not_cross_symbols():
    es, nq = _Recorder("ES"), _Recorder("NQ")
    dispatch_signal([es, nq], Signal(1, "NQ", "NQ:x", 1, 1, "entry", 1.0), emitter=None)
    assert es.seen == [] and len(nq.seen) == 1


def test_signal_carries_intent_not_position():
    """A Signal must not expose the peer's book -- that is the runaway vector."""
    fields = set(Signal.__slots__)
    assert "qty" in fields and "side" in fields and "source" in fields
    for banned in ("pos", "position", "avg_px", "book", "equity"):
        assert banned not in fields


def test_peer_reply_is_not_rebroadcast():
    """dispatch_signal returns replies; it must not feed them back in. One
    order -> at most one round of peer reaction."""
    e1, e2 = _Echo(), _Echo()
    out = dispatch_signal([e1, e2], Signal(1, "ES", "ES:src", 1, 1, "entry", 100.0))
    assert len(out) == 2                 # both replied once
    assert e1.calls == 1 and e2.calls == 1


# ── the real use: sweepfade exits when a watched peer opens against it ──────
def _sweep(t0, px0, n, aggressor=1):
    return [Trade(t0, px0 + aggressor * i * 0.25, 1, aggressor, "ES") for i in range(n)]


def _enter_short(s):
    t0 = _ts("2026-07-27 10:00:00")
    from engine.core.events import PositionUpdate
    pos = 0
    for t in _sweep(t0, 7000.0, 9) + [Trade(t0 + 5 * MS, 7002.0, 1, -1, "ES")]:
        for o in s.on_trade(t):
            pos += o.side * o.qty
            s.on_position(PositionUpdate(t.ts, "ES", pos, t.price))
    return pos


def test_peer_entry_against_us_closes_the_trade():
    s = SweepFollowStrategy("ES", min_span_ticks=6, peer_exit=("ES:onbreak",))
    assert _enter_short(s) == -1                      # short, fading an up-sweep
    out = s.on_signal(Signal(1, "ES", "ES:onbreak", +1, 1, "entry-onbreak", 7002.0))
    assert out and out[0].side == +1 and out[0].reduce_only
    assert out[0].tag == "swp-peer-onbreak"


def test_unwatched_peer_and_agreeing_peer_are_ignored():
    s = SweepFollowStrategy("ES", min_span_ticks=6, peer_exit=("ES:onbreak",))
    _enter_short(s)
    # not in peer_exit
    assert s.on_signal(Signal(1, "ES", "ES:zones", +1, 1, "entry", 7002.0)) == []
    # watched, but AGREES with our short
    assert s.on_signal(Signal(1, "ES", "ES:onbreak", -1, 1, "entry", 7002.0)) == []


def test_peer_exit_signal_is_not_a_reason_to_exit():
    """A peer CLOSING tells us nothing about direction."""
    s = SweepFollowStrategy("ES", min_span_ticks=6, peer_exit=("ES:onbreak",))
    _enter_short(s)
    sig = Signal(1, "ES", "ES:onbreak", +1, 1, "moc", 7002.0, True)   # reduce_only
    assert s.on_signal(sig) == []
