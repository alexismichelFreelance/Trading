"""Fast (DB-free) smoke tests: each live strategy instantiates, satisfies the
Strategy protocol, and processes synthetic events without error."""
import pandas as pd

from engine.core.events import BUY, SELL, Bar, BookFlow, Trade  # noqa: F401
from engine.core.ports import Strategy
from engine.strategies.flow import FlowFollowingStrategy
from engine.strategies.zones_strategy import ZoneLifecycleStrategy

NS = 1_000_000_000
T0 = pd.Timestamp("2025-04-01T14:00:00Z").value


def test_flow_strategy_protocol_and_run():
    s = FlowFollowingStrategy("ESM5")
    assert isinstance(s, Strategy)
    for i in range(300):
        ts = T0 + i * NS
        s.on_trade(Trade(ts, 5000.0 + i * 0.01, 400, BUY if i % 2 else SELL))
        out = s.on_bookflow(BookFlow(ts, 0, 0, 0, 0))
        assert isinstance(out, list)


def test_flow_session_reset_flattens():
    s = FlowFollowingStrategy("ESM5")
    s.pos = 7                                  # pretend we hold a position
    day2 = pd.Timestamp("2025-04-02T14:00:00Z").value
    s.on_trade(Trade(day2, 5000.0, 400, BUY))
    orders = s.on_bookflow(BookFlow(day2, 0, 0, 0, 0))
    assert any(o.tag == "session-flat" and o.reduce_only for o in orders)


def test_adaptive_flow_survives_dead_tape_then_fires():
    # REGRESSION (run_live ZeroDivisionError): a dead-quiet 30-min window makes
    # the adaptive threshold 0 -> scale 0 -> F/scale crashed. Must emit nothing
    # on dead tape, never crash, and still respond to a genuine surge later.
    # vol_win must dwarf the surge length (as live: 30min vs ~2min bursts),
    # else the threshold chases the surge itself and never fires.
    s = FlowFollowingStrategy("ESM5", maxp=5, adaptive=True, adapt_k=4.0,
                              vol_win=600, warm=60)
    # 100 seconds of ZERO flow (no trades at all) -> th<=0 path (the crash)
    for i in range(100):
        out = s.on_bookflow(BookFlow(T0 + i * NS, 0, 0, 0, 0))
        assert out == []                                  # no crash, no orders
    # mild two-sided tape to establish a small baseline
    for i in range(100, 300):
        s.on_trade(Trade(T0 + i * NS, 5000.0, 3, BUY if i % 2 else SELL))
        s.on_bookflow(BookFlow(T0 + i * NS, 0, 0, 0, 0))
    orders = []
    for i in range(300, 360):                             # 60s one-sided surge
        s.on_trade(Trade(T0 + i * NS, 5000.0 + i * 0.05, 120, BUY))
        orders += s.on_bookflow(BookFlow(T0 + i * NS, 0, 0, 0, 0))
    assert any(o.side == 1 for o in orders)               # the surge fires


def test_zone_strategy_protocol_and_run():
    s = ZoneLifecycleStrategy("ESM5")
    assert isinstance(s, Strategy)
    # feed 200 one-minute bars (close-stamped) -> exercises 30m aggregation + detector
    for m in range(200):
        ts = T0 + (m + 1) * 60 * NS
        c = 5000.0 + (m % 20)
        out = s.on_bar(Bar(ts, "1m", c, c + 1, c - 1, c, 100))
        assert isinstance(out, list)


def test_ibs_swing_buys_weak_close_sells_strong_close():
    from engine.strategies.ibs_swing import IBSSwingStrategy
    s = IBSSwingStrategy("ES")
    assert isinstance(s, Strategy)
    day1 = pd.Timestamp("2025-04-01T13:30:00Z").value      # 9:30 ET (EDT)
    # weak close: session range 5000-5020, close at 5001 -> IBS ~0.05
    orders = []
    for m in range(390):
        ts = day1 + m * 60 * NS
        px = 5020.0 - m * 0.049                             # drifts to ~5001
        orders += s.on_bar(Bar(ts, "1m", px, max(px, 5020.0) if m == 0 else px + 0.2,
                               px - 0.2 if m else 5000.0, px, 50))
    entry = [o for o in orders if o.tag == "ibs-entry"]
    assert len(entry) == 1 and entry[0].side == 1
    s.pos = 1
    # next day strong close: range 5000-5020, close 5019 -> IBS ~0.95 -> exit
    day2 = pd.Timestamp("2025-04-02T13:30:00Z").value
    orders = []
    for m in range(390):
        ts = day2 + m * 60 * NS
        px = 5000.0 + m * 0.049
        orders += s.on_bar(Bar(ts, "1m", px, px + 0.2, px - 0.2, px, 50))
    exits = [o for o in orders if o.tag == "ibs-exit"]
    assert len(exits) == 1 and exits[0].reduce_only


def test_open_drive_enters_at_10_et_and_flattens():
    from engine.strategies.open_drive import OpenDriveStrategy
    s = OpenDriveStrategy("ESM5")
    assert isinstance(s, Strategy)
    # 2025-04-01 (EDT): 9:30 ET = 13:30 UTC. Rising first 30 min -> long at 10:00.
    t930 = pd.Timestamp("2025-04-01T13:30:00Z").value
    orders = []
    for sec in range(0, 1900, 10):                     # 9:30 -> ~10:01 ET
        px = 5000.0 + sec * 0.01                       # steady drift up
        orders += s.on_trade(Trade(t930 + sec * NS, px, 1, BUY))
    entry = [o for o in orders if o.tag == "entry-opendrive"]
    assert len(entry) == 1 and entry[0].side == 1
    s.pos = 1
    # crash far below the stop -> trail/stop exit fires
    out = s.on_trade(Trade(t930 + 2000 * NS, 4900.0, 1, SELL))
    assert any(o.tag == "trail" and o.reduce_only for o in out)
