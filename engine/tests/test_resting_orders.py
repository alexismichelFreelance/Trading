"""A stop must fill AT ITS LEVEL, not wherever price happens to be later.

2026-07-31, ES:zones_gap: sized for a $2,000 loss on a ~7.5pt stop (5 contracts
at $50/pt) and lost $12,512 -- 6x its own risk budget. Not bad luck. The sleeve
detects the breach on a 1-MINUTE BAR CLOSE and then sends a MARKET order, and
`_paper_submit` fills every order at `px_for(symbol)` -- the price *now*. Price
ran from the stop (~7494) to 7466 inside that minute, so the engine noticed the
stop and sold 25 points below it.

Order already models OrderType.STOP with stop_price and validates it. The fill
model simply ignored all of it, which is why every sleeve emits MARKET and
re-implements stops by hand. So the fix is not a monitor and not a per-sleeve
patch: the paper broker holds RESTING orders and fills them when the market
trades through their level. Then "the stop did not hold" is not a state the
engine can be in.

Slippage is modelled explicitly (a stop is not a guaranteed price) but it is a
declared, bounded number rather than "however far price got before the next bar
closed".
"""
from __future__ import annotations

import asyncio

from engine.core.blotter import Blotter
from engine.core.clock import WallClock
from engine.core.events import Trade
from engine.core.live_engine import LiveEngine
from engine.core.orders import Order, OrderType

NS = 1_000_000_000
TICK = 0.25


def _mid_session() -> int:
    """A fixed 11:00 ET instant, well inside RTH.

    NOT time.time_ns(). The engine flattens intraday sleeves during the
    15:59-18:00 ET closing window, so a test anchored to "now" silently gains an
    extra flatten fill when it happens to run in that window -- it would pass or
    fail by time of day. Anchoring makes it deterministic.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return int(datetime(2026, 7, 31, 11, 0,
                        tzinfo=ZoneInfo("America/New_York")).timestamp() * 1e9)



class Sleeve:
    """Buys once, then rests a protective stop. No bar-close stop logic."""
    symbol = "ES"

    def __init__(self, stop_px: float) -> None:
        self.stop_px = stop_px
        self.done = False
        self.fills: list = []

    def on_trade(self, t):
        if self.done:
            return []
        self.done = True
        return [
            Order("ES", 1, 5, tag="entry"),
            Order("ES", -1, 5, type=OrderType.STOP, stop_price=self.stop_px,
                  tag="protective-stop", reduce_only=True),
        ]

    def on_fill(self, f):
        self.fills.append(f)

    def on_position(self, p):
        return None


class ListFeed:
    finite = True

    def __init__(self, prices, base):
        self.prices, self.base = prices, base

    async def stream(self):
        for i, p in enumerate(self.prices):
            yield Trade(self.base + i * NS, p, 1, 1, symbol="ES")


class NullBroker:
    finite = True

    async def submit(self, o):
        return None

    async def events(self):
        return
        yield


def _run(prices, stop_px, **kw):
    base = _mid_session()
    s = Sleeve(stop_px)
    eng = LiveEngine(ListFeed(prices, base), NullBroker(), [s], WallClock(),
                     Blotter("ES", 50.0), warmup_gate=False, live_owners=set(), **kw)
    asyncio.run(eng.run())
    return s, eng


def _by_tag(eng, tag):
    return [f for f in eng.paper_fills if f.tag == tag]


def _walk(a: float, b: float) -> list[float]:
    """Every tick from a down/up to b -- what a liquid future actually does. ES
    does not gap 29 points inside a minute; it TRADED through every one of them
    while the engine was only looking at bar closes."""
    n = int(round(abs(b - a) / TICK))
    step = TICK if b > a else -TICK
    return [round(a + i * step, 2) for i in range(n + 1)]


def test_stop_fills_at_its_level_not_at_the_far_side_of_the_move():
    """THE REPRODUCTION: enter 7502, stop 7494.50, price then TRADES down to 7466
    tick by tick. The stop must fill at ~7494.50, not 25 points later."""
    prices = _walk(7502.0, 7466.0)
    s, eng = _run(prices, stop_px=7494.50)
    stops = _by_tag(eng, "protective-stop")
    assert len(stops) == 1, f"stop did not fill exactly once: {len(stops)}"
    px = stops[0].price
    assert px <= 7494.50, "a sell stop cannot fill above its level"
    assert px >= 7494.50 - 4 * TICK, (
        f"stop filled at {px}, {7494.50 - px:.2f} points past its level "
        f"(the 2026-07-31 zones_gap defect)")


def test_loss_is_bounded_by_the_risk_the_size_was_chosen_for():
    """The point of risk-based sizing: 5 contracts x ~7.5pt stop x $50 = ~$2,000.
    The realised loss must be that, not a multiple of it."""
    entry_px, stop_px = 7502.0, 7494.50
    s, eng = _run(_walk(entry_px, 7466.0), stop_px=stop_px)
    entries = _by_tag(eng, "entry")
    stops = _by_tag(eng, "protective-stop")
    loss = (stops[0].price - entries[0].price) * 5 * 50.0
    assert -2600.0 <= loss <= 0.0, (
        f"lost ${-loss:,.0f} on a position sized to risk ~$2,000")


def test_stop_does_not_fire_before_the_market_reaches_it():
    prices = [7502.0, 7500.0, 7496.0, 7498.0, 7501.0]      # never touches 7494.50
    s, eng = _run(prices, stop_px=7494.50)
    assert _by_tag(eng, "protective-stop") == [], "stop fired without being hit"
    assert eng.strategy_position(s) == 5


def test_buy_stop_is_the_mirror():
    class ShortSleeve(Sleeve):
        def on_trade(self, t):
            if self.done:
                return []
            self.done = True
            return [Order("ES", -1, 2, tag="entry"),
                    Order("ES", 1, 2, type=OrderType.STOP, stop_price=self.stop_px,
                          tag="protective-stop", reduce_only=True)]

    base = _mid_session()
    s = ShortSleeve(7510.0)
    eng = LiveEngine(ListFeed(_walk(7502.0, 7550.0), base), NullBroker(),
                     [s], WallClock(), Blotter("ES", 50.0), warmup_gate=False,
                     live_owners=set())
    asyncio.run(eng.run())
    stops = _by_tag(eng, "protective-stop")
    assert len(stops) == 1
    assert stops[0].price >= 7510.0, "a buy stop cannot fill below its level"
    assert stops[0].price <= 7510.0 + 4 * TICK, (
        f"buy stop filled at {stops[0].price}, far above its level")


def test_a_real_gap_through_the_stop_fills_at_the_first_available_price():
    """The honest counterpart: if the tape truly JUMPS the level with no trade in
    between, filling past it is correct, not a bug. This must not be papered over
    with a fictional fill at the stop price."""
    prices = [7502.0, 7500.0, 7495.0, 7466.0, 7460.0]      # a genuine 29pt gap
    s, eng = _run(prices, stop_px=7494.50)
    stops = _by_tag(eng, "protective-stop")
    assert len(stops) == 1
    assert stops[0].price == 7466.0, (
        "a gapped stop must fill at the first traded price through the level, "
        "not at a price that never traded")


def test_market_orders_are_unchanged():
    """The existing behaviour every other sleeve relies on must not move."""
    prices = [7502.0, 7500.0, 7495.0]
    s, eng = _run(prices, stop_px=1.0)          # stop unreachable
    entries = _by_tag(eng, "entry")
    assert len(entries) == 1 and entries[0].price == 7502.0


def test_a_filled_stop_does_not_fill_twice():
    s, eng = _run(_walk(7502.0, 7460.0), stop_px=7494.50)
    assert len(_by_tag(eng, "protective-stop")) == 1
    assert eng.strategy_position(s) == 0
