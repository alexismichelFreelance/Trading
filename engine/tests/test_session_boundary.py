"""No sleeve may hold a position past the session unless it SAYS it is a swing
sleeve, and no time gate may be expressed in UTC hours.

Three defects found 2026-08-01 by auditing every sleeve for session handling:

  * zones_strategy.py had NO session awareness at all -- no et_session_date, no
    daily state reset, no end-of-day flat. Its only exit bound is K_BARS=16 bars,
    so a position entered near the close rides through the close, through the
    17:00 ET Globex halt, and into the next session. It is also the only sleeve
    that sizes by risk (5+ contracts), i.e. the largest position in the roster.
  * ignition.py has no end-of-day flat either; ES:ignition_fixed was still open
    at 16:00 on 2026-07-31.
  * flow.py gates on `gate_utc=(13, 21)` compared against a UTC hour. A fixed UTC
    hour cannot express "16:00 ET": 21 UTC is 17:00 ET in summer and 16:00 ET in
    winter, so the window silently moves by an hour twice a year.

The fix is opt-OUT, not opt-in: the engine flattens at the session boundary
unless a sleeve declares `holds_overnight = True`. Forgetting then means being
flattened, which is the safe direction. IBS is a swing sleeve and declares it.
"""
from __future__ import annotations

import time

import pytest

from engine.core.events import Bar, Trade
from engine.core.timeutil import et_minute_of_day

NS = 1_000_000_000


def _ns_at_et(y, m, d, hh, mm) -> int:
    """Epoch ns for a wall-clock ET time, DST handled by the zone database."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    dt = datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("America/New_York"))
    return int(dt.timestamp() * 1e9)


# ── the DST defect, stated as arithmetic ────────────────────────────────────
def test_a_utc_hour_gate_cannot_mean_a_fixed_et_hour():
    """Same UTC hour, two different ET hours, six months apart. This is why a
    UTC-hour window is not a session window."""
    from engine.core.timeutil import ns_to_utc
    summer = _ns_at_et(2026, 7, 31, 16, 0)          # 16:00 ET, EDT
    winter = _ns_at_et(2026, 12, 15, 16, 0)         # 16:00 ET, EST
    assert ns_to_utc(summer).hour == 20
    assert ns_to_utc(winter).hour == 21
    # ...so a gate that closes at UTC hour 20 is correct in July and shuts an
    # hour EARLY in December; one that closes at 21 is an hour LATE in July.
    assert et_minute_of_day(summer) == et_minute_of_day(winter) == 16 * 60


def test_flow_window_closes_at_the_same_ET_time_all_year():
    from engine.strategies.flow import FlowFollowingStrategy
    # (13, 20) is what run_live uses in production: 09:00-16:00 ET.
    f = FlowFollowingStrategy("ES", maxp=5, gate_utc=(13, 20))
    for ns in (_ns_at_et(2026, 7, 31, 15, 59), _ns_at_et(2026, 12, 15, 15, 59)):
        assert f.in_window(ns), "flow shut before the close"
    for ns in (_ns_at_et(2026, 7, 31, 16, 1), _ns_at_et(2026, 12, 15, 16, 1)):
        assert not f.in_window(ns), "flow still open after the close"
    # and the class default keeps its replay meaning (09:00-17:00 ET) in BOTH
    # halves of the year -- the 2025 parity data is all EDT, so this is unchanged
    # there, while December no longer silently shifts by an hour.
    d = FlowFollowingStrategy("ES", maxp=5)
    for ns in (_ns_at_et(2026, 7, 31, 16, 30), _ns_at_et(2026, 12, 15, 16, 30)):
        assert d.in_window(ns)
    for ns in (_ns_at_et(2026, 7, 31, 17, 1), _ns_at_et(2026, 12, 15, 17, 1)):
        assert not d.in_window(ns)


# ── the session-boundary contract ───────────────────────────────────────────
def test_every_intraday_sleeve_declares_its_overnight_intent():
    """A sleeve either flattens at the session boundary or says it is a swing
    sleeve. There is no third option, and the default is the safe one."""
    from engine.strategies.base import BaseStrategy
    assert BaseStrategy.holds_overnight is False, "the default must be SAFE"


@pytest.mark.parametrize("mod,expect", [
    (15 * 60 + 58, False),     # 15:58 - still trading
    (15 * 60 + 59, True),      # 15:59 - flat
    (16 * 60 + 30, True),      # after the close
])
def test_session_flat_boundary(mod, expect):
    from engine.strategies.base import BaseStrategy
    ns = _ns_at_et(2026, 7, 31, mod // 60, mod % 60)
    assert BaseStrategy.session_over(ns) is expect


def test_zones_gap_flattens_at_the_close_instead_of_riding_overnight():
    """THE REPRODUCTION for the largest-sized sleeve in the roster."""
    from engine.strategies.zones_strategy import ZoneLifecycleStrategy
    z = ZoneLifecycleStrategy("ES", point_usd=50.0)
    assert z.holds_overnight is False
    # pretend it is holding 5 long when the close arrives
    z.pos = 5
    z.trade = object()
    out = z.on_bar(Bar(_ns_at_et(2026, 7, 31, 15, 59), "1m",
                       7500.0, 7501.0, 7499.0, 7500.0, 100, "ES"))
    assert out, "zones_gap did not flatten at the session close"
    o = out[0]
    assert o.side == -1 and o.qty == 5 and o.reduce_only
    assert "flat" in o.tag or "moc" in o.tag


def test_ignition_flattens_at_the_close():
    from engine.strategies.ignition import IgnitionStrategy
    assert IgnitionStrategy.holds_overnight is False


def test_ibs_is_allowed_to_hold_overnight_because_it_says_so():
    """The opt-out must exist, or the rule would break the genuine swing sleeve."""
    from engine.strategies.ibs_swing import IBSSwingStrategy
    assert IBSSwingStrategy.holds_overnight is True


def test_zones_gap_resets_its_state_each_session():
    """No et_session_date anywhere in that file meant trade/zone state carried
    silently from one session into the next."""
    from engine.strategies.zones_strategy import ZoneLifecycleStrategy
    z = ZoneLifecycleStrategy("ES", point_usd=50.0)
    d1 = _ns_at_et(2026, 7, 30, 11, 0)
    z.on_bar(Bar(d1, "1m", 7500.0, 7501.0, 7499.0, 7500.0, 100, "ES"))
    seen = z._day
    d2 = _ns_at_et(2026, 7, 31, 11, 0)
    z.on_bar(Bar(d2, "1m", 7500.0, 7501.0, 7499.0, 7500.0, 100, "ES"))
    assert z._day != seen, "zones_gap never noticed the session changed"


# ── the contract has to be ENFORCED, not merely declared ────────────────────
def test_engine_flattens_any_sleeve_that_is_still_holding_at_the_close():
    """Declaring `holds_overnight = False` must not depend on each sleeve
    remembering to act on it -- that is exactly the per-sleeve discipline that
    failed. The ENGINE flattens, so a sleeve written tomorrow gets it for free
    and a sleeve that forgets cannot carry a position overnight."""
    import asyncio

    from engine.core.blotter import Blotter
    from engine.core.clock import WallClock
    from engine.core.live_engine import LiveEngine
    from engine.core.orders import Order

    class Forgetful:
        """Enters and never exits -- the ignition/zones failure mode."""
        symbol = "ES"
        holds_overnight = False

        def __init__(self):
            self.n = 0

        def on_trade(self, t):
            self.n += 1
            return [Order("ES", 1, 3, tag="entry")] if self.n == 1 else []

        def on_fill(self, f):
            return None

        def on_position(self, p):
            return None

    class Swing(Forgetful):
        holds_overnight = True

    class ListFeed:
        finite = True

        def __init__(self, ev):
            self.ev = ev

        async def stream(self):
            for e in self.ev:
                yield e

    class NullBroker:
        finite = True

        async def submit(self, o):
            return None

        async def events(self):
            return
            yield

    # 15:50 ET (trading) then 16:05 ET (past the close)
    ev = [Trade(_ns_at_et(2026, 7, 31, 15, 50), 7500.0, 1, 1, symbol="ES"),
          Trade(_ns_at_et(2026, 7, 31, 16, 5), 7490.0, 1, 1, symbol="ES")]
    a, b = Forgetful(), Swing()
    eng = LiveEngine(ListFeed(ev), NullBroker(), [a, b], WallClock(),
                     Blotter("ES", 50.0), warmup_gate=False, live_owners=set())
    asyncio.run(eng.run())
    assert eng.strategy_position(a) == 0, "intraday sleeve carried a position overnight"
    assert eng.strategy_position(b) == 3, "swing sleeve was flattened against its will"
    flat = [f for f in eng.paper_fills if "session-flat" in f.tag]
    assert len(flat) == 1 and flat[0].size == -3


def test_the_closing_window_is_bounded_by_the_globex_reopen():
    """`>= 15:59` is not the rule. At 20:00 ET the session date has already
    rolled and the position belongs to a NEW session -- an unbounded rule
    force-closed overnight entries the moment they were opened."""
    from engine.strategies.base import BaseStrategy as B
    assert B.session_over(_ns_at_et(2026, 7, 31, 15, 59))    # closing
    assert B.session_over(_ns_at_et(2026, 7, 31, 17, 30))    # halt
    assert not B.session_over(_ns_at_et(2026, 7, 31, 18, 0))  # Globex reopen
    assert not B.session_over(_ns_at_et(2026, 7, 31, 20, 0))  # overnight trading
    assert not B.session_over(_ns_at_et(2026, 7, 31, 3, 0))   # Europe
