"""A stop exit fills AT THE STOP, never at the close of the bar that tripped it.

Every sleeve that carries a stop detects it the same way -- on the bar:

    stop_hit = bar.l <= t["stop"] if d > 0 else bar.h >= t["stop"]
    if stop_hit:
        return self._flatten("stop")          # ...a MARKET order

and a MARKET order is priced at the engine's last price, which on a bar-driven
exit is that bar's CLOSE. So the fill is wherever price happened to be when the
bar ended -- not where the stop was.

Measured, live, 2026-08-12: ES:pivot went long 7765.25 at 10:31 ET. The next
minute traded down to 7757.50 (through the stop) and closed back at 7769.25.
The sleeve booked 7769.25 -- +4.00 points on a trade that was stopped out. The
stop was somewhere at or above 7757.50 and below the 7765.25 entry, so the true
result is a LOSS of up to 8 points, and the record says +$200.

This is not a pivot bug. pivot, zones, dip_buy and vwap_break all detect on the
bar and all exit at the market, in replay (SimBroker prices MARKET at
`ref_price`, the last close) exactly as in live. Every stop in the book, in
every number the scorecard has ever produced, is priced at the bar close.

The direction of the error is not random. A stop tripped by a wick that closes
back is priced BETTER than the stop; a stop tripped by a bar that keeps going is
priced worse. The first case is the one that recurs, because a wick through a
level and back is the ordinary shape of a stop run. The bias flatters.

THE RULE: an exit caused by the tape reaching a level fills at that level. The
sleeve holds the bar, so it also knows when the bar GAPPED past the level (open
already beyond it) -- then the open is the honest fill and the difference is
real slippage. It states that price on the order as `trigger_price`; both fill
paths, live paper and SimBroker, honour it.

Deliberately NOT changed here: exits are still detected at bar close and still
sent as MARKET orders. Resting the stop as a real bracket order would also fix
the TIMING (the exit would fire the instant the tape trades through, not up to
59 seconds later), but it needs OCO cancellation that the engine does not have
yet -- an orphaned resting stop would sit in the book and reduce a later,
unrelated position of the same sleeve. Pricing is fixed here; timing is next.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.brokers.sim import SimBroker  # noqa: E402
from engine.core.blotter import Blotter  # noqa: E402
from engine.core.clock import EventClock  # noqa: E402
from engine.core.events import BUY, Bar, PositionUpdate, Trade  # noqa: E402
from engine.core.live_engine import LiveEngine  # noqa: E402
from engine.core.orders import Order, OrderType  # noqa: E402
from engine.strategies.pivot import PivotStrategy  # noqa: E402

NS = 1_000_000_000
T0 = 1_785_949_200 * NS


def _b(t, o, h, l, c, v=1000, sym="ES"):
    return Bar(int(pd.Timestamp(t, tz="America/New_York").value), "1m",
               o, h, l, c, v, sym)


# ── 1. the live paper fill path ──────────────────────────────────────────────

class _Once:
    symbol = "ES"

    def __init__(self, order):
        self.order, self.sent = order, False

    def on_trade(self, e):
        if self.sent:
            return []
        self.sent = True
        return [self.order]

    def on_fill(self, f):
        return None

    def on_position(self, p):
        return None


class _Feed:
    finite = True

    def __init__(self, evs):
        self.evs = evs

    async def stream(self):
        for e in self.evs:
            yield e


class _Broker:
    async def submit(self, o):
        return None

    async def events(self):
        return
        yield


def _paper_fill(order, prints, seed_pos=0, seed_px=0.0):
    s = _Once(order)
    evs = [Trade(T0 + i * NS, p, 1, BUY, "ES") for i, p in enumerate(prints)]

    async def go():
        eng = LiveEngine(_Feed(evs), _Broker(), [s], EventClock(),
                         Blotter("ES", 50.0), live_owners=set())
        if seed_pos:
            eng._spos[id(s)] = seed_pos
            eng._savg[id(s)] = seed_px
        await eng.run()
        return eng

    eng = asyncio.run(go())
    return [f for f in eng.paper_fills if f.tag == "x"]


def test_paper_market_exit_honours_the_trigger_price():
    """THE REGRESSION. Long 7765.25, stop 7761.00, bar closes back at 7769.25.
    The exit must book 7761.00, not the 7769.25 the tape happens to show."""
    o = Order("ES", -1, 1, tag="x", reduce_only=True, trigger_price=7761.0)
    f = _paper_fill(o, [7769.25], seed_pos=1, seed_px=7765.25)
    assert f, "reduce_only exit never filled"
    assert f[0].price == 7761.0, (
        f"stop exit booked {f[0].price} -- that is the bar close. The stop was "
        f"at 7761.00, so this hands the sleeve {f[0].price - 7761.0:+.2f} points "
        f"it could never have had.")


def test_a_market_exit_without_a_trigger_is_unchanged():
    """Timeouts, session-flat and discretionary exits are genuinely 'at the
    market'. They must keep taking the market price."""
    o = Order("ES", -1, 1, tag="x", reduce_only=True)
    f = _paper_fill(o, [7769.25], seed_pos=1, seed_px=7765.25)
    assert f and f[0].price == 7769.25


def test_the_trigger_price_is_used_even_when_it_is_the_worse_side():
    """Not a floor on losses -- the level is the fill, whichever way it cuts.
    A bar that trips the stop and keeps falling still fills at the stop, because
    that is where the order was; a gap is the sleeve's job to report (below)."""
    o = Order("ES", -1, 1, tag="x", reduce_only=True, trigger_price=7761.0)
    f = _paper_fill(o, [7750.0], seed_pos=1, seed_px=7765.25)
    assert f and f[0].price == 7761.0


# ── 2. the replay path ───────────────────────────────────────────────────────

def test_simbroker_honours_the_trigger_price():
    """Replay must agree with live or the scorecard measures a different engine
    from the one that trades."""
    async def go():
        b = SimBroker("ES", EventClock())
        b.on_market_event(_b("2026-07-23 10:31", 7772, 7772.75, 7757.5, 7769.25))
        b.qty = 1
        await b.submit(Order("ES", -1, 1, tag="x", reduce_only=True,
                             trigger_price=7761.0))
        return b.drain()

    evs = asyncio.run(go())
    fills = [e for e in evs if getattr(e, "price", None) is not None]
    assert fills, "SimBroker produced no fill"
    assert fills[0].price == 7761.0, (
        f"replay booked {fills[0].price} (ref_price = the bar close); live now "
        f"books 7761.00. Two engines, two answers.")


def test_simbroker_market_without_a_trigger_is_unchanged():
    async def go():
        b = SimBroker("ES", EventClock())
        b.on_market_event(_b("2026-07-23 10:31", 7772, 7772.75, 7757.5, 7769.25))
        b.qty = 1
        await b.submit(Order("ES", -1, 1, tag="x", reduce_only=True))
        return b.drain()

    fills = [e for e in asyncio.run(go()) if getattr(e, "price", None) is not None]
    assert fills and fills[0].price == 7769.25


# ── 3. the sleeves state the level ───────────────────────────────────────────

def _armed_short():
    s = PivotStrategy("ES")
    s.on_bar(_b("2026-07-22 12:00", 7500, 7550, 7450, 7460))
    px = 7480.0
    for i in range(12):
        px -= 4
        s.on_bar(_b(f"2026-07-23 0{i // 60}:{i % 60:02d}", px + 1, px + 2, px - 1, px))
    for i in range(12, 40):
        hh, mm = divmod(i, 60)
        s.on_bar(_b(f"2026-07-23 0{hh}:{mm:02d}", 7436, 7438, 7434, 7436))
    s.on_bar(_b("2026-07-23 09:30", 7436, 7440, 7434, 7438))
    return s


def _pivot_in_a_short():
    s = _armed_short()
    orders = s.on_bar(_b("2026-07-23 09:45", 7470, 7490, 7465, 7478))
    assert [o for o in orders if o.tag == "piv-entry"], "no pivot entry"
    s.on_position(PositionUpdate(T0, "ES", -1, s.trade["entry"]))
    return s


def test_pivot_stop_carries_the_stop_level():
    """THE LIVE CASE, mirrored: the bar spikes THROUGH the stop and closes back
    on the good side. Booking the close is a phantom win."""
    s = _pivot_in_a_short()
    stop = s.trade["stop"]
    out = s.on_bar(_b("2026-07-23 10:00", stop - 6, stop + 2, stop - 8, stop - 6))
    ex = [o for o in out if o.tag == "piv-stop"]
    assert ex, f"no stop exit; got {[o.tag for o in out]}"
    assert ex[0].trigger_price is not None, (
        "piv-stop is a bare MARKET order -- it will fill at the bar close "
        f"({stop - 6}), {stop - (stop - 6):+.2f} points better than the stop")
    assert ex[0].trigger_price == stop


def test_pivot_target_carries_the_target_level():
    """Same defect, opposite sign: a target reached intrabar and given back by
    the close is booked at the close, i.e. WORSE than the target that triggered
    it. The level is the fill, both ways."""
    s = _pivot_in_a_short()
    tgt = s.trade["target"]
    out = s.on_bar(_b("2026-07-23 10:00", tgt + 6, tgt + 8, tgt - 2, tgt + 6))
    ex = [o for o in out if o.tag == "piv-target"]
    assert ex, f"no target exit; got {[o.tag for o in out]}"
    assert ex[0].trigger_price == tgt


def test_a_gap_straight_through_the_stop_is_real_slippage():
    """The one case where the level is NOT the fill. If the bar OPENED beyond
    the stop there was never a print at it, so the open is the honest fill and
    the difference is slippage the sleeve really would have paid."""
    s = _pivot_in_a_short()
    stop = s.trade["stop"]
    gap = stop + 15
    out = s.on_bar(_b("2026-07-23 10:00", gap, gap + 2, gap - 1, gap + 1))
    ex = [o for o in out if o.tag == "piv-stop"]
    assert ex, f"no stop exit; got {[o.tag for o in out]}"
    assert ex[0].trigger_price == gap, (
        f"gapped open {gap} but claims a fill at {ex[0].trigger_price} -- the "
        f"stop never traded, so that is {gap - ex[0].trigger_price:.2f} points "
        f"of slippage written out of existence")


def test_pivot_timeout_and_session_flat_stay_at_the_market():
    """Not every exit has a level. EOD flat is genuinely at the market and must
    not acquire a phantom one."""
    s = _pivot_in_a_short()
    out = s.on_bar(_b("2026-07-23 15:59", 7460, 7465, 7455, 7460))
    for o in out:
        if o.tag.endswith("-flat") or "timeout" in o.tag:
            assert o.trigger_price is None, f"{o.tag} invented a level"
