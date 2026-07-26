"""SweepFollowStrategy: clusters the tape without order_id, fires only on deep
sweeps, enters AFTER the cluster ends, and exits on the clock because the edge
is transient."""
import pandas as pd

from engine.core.events import PositionUpdate, Trade
from engine.strategies.sweep_follow import SweepFollowStrategy

MS = 1_000_000


def _ts(et_str):
    return int(pd.Timestamp(et_str, tz="America/New_York").value)


def _drive(s, trades):
    orders, pos = [], 0
    for t in trades:
        for o in s.on_trade(t):
            orders.append(o)
            pos += o.side * o.qty
            s.on_position(PositionUpdate(t.ts, s.symbol, pos, t.price))
    return orders, pos


def _sweep(t0, px0, n, aggressor=1, step=0.25, gap=0):
    """n fills walking the book, all inside one cluster."""
    return [Trade(t0 + i * gap, px0 + aggressor * i * step, 1, aggressor, "ES")
            for i in range(n)]


def test_deep_sweep_fires_after_cluster_ends_and_FADES_it():
    """Default mode is fade: an UP sweep produces a SHORT. Following was
    measured to lose on every parameter (win rate 21-41%)."""
    t0 = _ts("2026-07-24 10:00:00")
    s = SweepFollowStrategy("ES", min_span_ticks=6)
    trades = _sweep(t0, 7000.0, 9) + [Trade(t0 + 5 * MS, 7002.0, 1, -1, "ES")]
    orders, pos = _drive(s, trades)
    e = next((o for o in orders if o.tag == "entry-sweep"), None)
    assert e is not None and e.side == -1        # AGAINST the up-sweep
    assert pos == -1


def test_follow_mode_takes_the_other_side():
    t0 = _ts("2026-07-24 10:00:00")
    s = SweepFollowStrategy("ES", min_span_ticks=6, mode="follow")
    trades = _sweep(t0, 7000.0, 9) + [Trade(t0 + 5 * MS, 7002.0, 1, -1, "ES")]
    orders, _ = _drive(s, trades)
    e = next(o for o in orders if o.tag == "entry-sweep")
    assert e.side == 1


def test_shallow_cluster_ignored():
    t0 = _ts("2026-07-24 10:00:00")
    s = SweepFollowStrategy("ES", min_span_ticks=6)
    trades = _sweep(t0, 7000.0, 3) + [Trade(t0 + 5 * MS, 7000.5, 1, -1, "ES")]
    orders, _ = _drive(s, trades)
    assert not [o for o in orders if o.tag == "entry-sweep"]


def test_gap_splits_a_cluster_so_it_never_looks_deep():
    """Same 9 prints, but spaced 3ms apart: with a 1ms window these are NINE
    clusters of one tick each, not one 8-tick sweep."""
    t0 = _ts("2026-07-24 10:00:00")
    s = SweepFollowStrategy("ES", min_span_ticks=6, gap_ms=1)
    trades = _sweep(t0, 7000.0, 9, gap=3 * MS) + [Trade(t0 + 60 * MS, 7002.0, 1, -1, "ES")]
    orders, _ = _drive(s, trades)
    assert not [o for o in orders if o.tag == "entry-sweep"]


def test_timeout_exit_because_the_edge_decays():
    t0 = _ts("2026-07-24 10:00:00")
    s = SweepFollowStrategy("ES", min_span_ticks=6, hold_s=15.0)
    trades = _sweep(t0, 7000.0, 9) + [Trade(t0 + 5 * MS, 7002.0, 1, -1, "ES")]
    trades.append(Trade(t0 + 20_000 * MS, 7002.5, 1, -1, "ES"))     # 20s later
    orders, pos = _drive(s, trades)
    assert any(o.tag == "swp-timeout" for o in orders)
    assert pos == 0


def test_stop_and_eod_flat():
    t0 = _ts("2026-07-24 10:00:00")
    s = SweepFollowStrategy("ES", min_span_ticks=6, stop_ticks=4)
    trades = _sweep(t0, 7000.0, 9) + [Trade(t0 + 5 * MS, 7002.0, 1, -1, "ES")]
    # fade of an up-sweep is SHORT from ~7002, so price UP is adverse
    trades.append(Trade(t0 + 100 * MS, 7004.0, 1, 1, "ES"))
    orders, pos = _drive(s, trades)
    assert any(o.tag == "swp-stop" for o in orders)
    assert pos == 0


def test_no_entries_before_rth():
    t0 = _ts("2026-07-24 08:00:00")
    s = SweepFollowStrategy("ES", min_span_ticks=6)
    trades = _sweep(t0, 7000.0, 9) + [Trade(t0 + 5 * MS, 7002.0, 1, -1, "ES")]
    orders, _ = _drive(s, trades)
    assert not [o for o in orders if o.tag == "entry-sweep"]
