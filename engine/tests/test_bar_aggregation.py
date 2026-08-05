"""BarAggregator: aggregated bars must keep their instrument, and a bar built
from a gapped bucket must not pretend to be complete."""
from engine.core.events import Bar
from engine.features.bars import BarAggregator

NS = 1_000_000_000
M = 60 * NS


def _bar(minute_idx, sym="ES", o=100.0):
    ts = minute_idx * M
    return Bar(ts, "1m", o, o + 1, o - 1, o + 0.5, 10, sym)


def test_aggregated_bars_keep_their_symbol():
    """Bar.symbol defaults to "" and dispatch treats "" as BROADCAST, so an
    aggregated bar with no symbol would be delivered to every lane's strategies."""
    a = BarAggregator(("30m",))
    out = []
    for i in range(1, 62):
        out += a.update(_bar(i, sym="NQ"))
    out += a.flush()
    assert out, "no 30m bars emitted"
    assert all(b.symbol == "NQ" for b in out), \
        f"aggregator dropped the symbol: {[b.symbol for b in out]}"


def test_a_gapped_bucket_is_not_reported_as_a_full_bar():
    """10 minutes of data then an outage: the 30m bar that comes out covers 10
    minutes, not 30. It must say so rather than look like a complete bar."""
    a = BarAggregator(("30m",))
    for i in range(1, 11):            # 00:00-00:10 only
        a.update(_bar(i))
    out = a.update(_bar(40))          # next bucket -> flushes the thin one
    assert len(out) == 1
    assert out[0].v == 100            # 10 bars x 10
    assert getattr(out[0], "complete", True) is False, \
        "a 10-minute bucket was emitted as a complete 30m bar"
