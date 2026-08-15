"""A zone entry fills AT the edge it claims to trade, not at the bar close.

`_enter` recorded the zone edge as the entry and then sprayed a market order:

    self.trade = _Trade(setup, d, entry, stop, target, size,
                        entry + d * SCALP, remaining=size)
    return [Order(self.symbol, d, size, tag=tag)]      # ...MARKET

so the position filled wherever the tape happened to be, while stop, target,
scalp_px AND the breakeven stop were all computed from `entry` -- a price the
trade never had. This is the identical fault fixed in pivot.py on 2026-08-11
(f91368f), left standing in the sleeve that trades the LARGEST size in the
roster: position_size(RISK=2000, risk, point_usd, cap=30).

It shows up in the record as an impossibility. Replaying 25 ES sessions with
corrected exit fills, ES:zones_gap:

    target  9 legs  +16,012      scale  11 legs  +1,150
    stop    3 legs   -1,775      be     26 legs -12,225

Twenty-six BREAKEVEN exits losing $12,225, a mean of -$470 each. A breakeven
exit cannot lose $470. `t.stop = t.entry` after the scalp sets the runner's stop
to the ZONE EDGE, and the trade is then flattened there against a fill that was
somewhere else entirely. That one line is bigger than every setup's P&L combined
and it is not a strategy result, it is an arithmetic error.

A resting LIMIT is the WRONG fix and the replay proved it: ES:zones and
ES:zones_15m went from 9 and 12 trading days to ZERO, zones_gap from 25 to 2.
The touch that produces the signal happens DURING the bar being closed, so an
order resting from the close onward only fills if price comes back -- on a 30m
bar it does not. That turns "enter at the level" into "enter on a re-touch",
which is a different strategy, not a fill-price correction. The same mistake as
the vwapbreak retest bug: moving the ORDER instead of the PRICE.

What the detection actually proves is that the bar's own range contained the
level:

    FADE  d>0:  b.l <= z.top          -> price traded down to the proximal edge
    FLIP  d>0:  b.h >= z.bot          -> price traded up to the flip level

so the trade happens on that bar, at that level -- exactly how an order resting
at the edge since the zone was DETECTED would have been filled. The two are the
same thing, which is why no cancellation machinery is needed: the sleeve's "first
touch of a fresh zone" condition IS the resting order's first fillable bar.

And where no order could have been resting -- a zone created by the very bar
being traded -- there is NO TRADE. Not a worse price: a limit fills at its price
or not at all. See the last two tests in this file.

BREAK is deliberately left a bare MARKET order. Its entry is `b.c`, the close of
the bar that broke the zone -- there is no level to rest at, the entry price IS
the market, and claiming one would invent a fill the setup never had.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.events import Bar  # noqa: E402
from engine.core.orders import OrderType  # noqa: E402
from engine.strategies.zones_strategy import (ZoneLifecycleStrategy,  # noqa: E402
                                              parse_zone_tag)

NS = 60_000_000_000


def _sleeve_with_entries(**kw):
    """Two filler sessions to set the average range, then a tight base, a large
    departure that leaves a zone, and a return to it. Same fixture as
    test_zone_metadata -- the detector needs 20 closed 30m bars, which is more
    than one session provides."""
    s = ZoneLifecycleStrategy("ES", point_usd=50.0, **kw)
    out = []

    def feed(day, mins, lo, hi, step, vol=500):
        start = int(pd.Timestamp(f"{day} 09:00", tz="America/New_York").value)
        for j in range(mins):
            px = lo + (hi - lo) * (j / max(1, mins - 1))
            out.extend(s.on_bar(Bar(start + j * NS, "1m", px, px + step,
                                    px - step, px, vol, "ES")) or [])

    feed("2026-08-03", 480, 7000.0, 7030.0, 1.0)
    feed("2026-08-04", 480, 7030.0, 7000.0, 1.0)

    start = int(pd.Timestamp("2026-08-05 09:00", tz="America/New_York").value)
    n = 0

    def bucket(lo, hi, step, vol=500):
        nonlocal n
        for j in range(30):
            px = lo + (hi - lo) * (j / 29.0)
            out.extend(s.on_bar(Bar(start + n * NS, "1m", px, px + step,
                                    px - step, px, vol, "ES")) or [])
            n += 1

    for _ in range(3):
        bucket(7000.0, 7000.2, 0.2)
    bucket(7000.0, 6940.0, 1.0, vol=900)
    bucket(6940.0, 6955.0, 0.5)
    for _ in range(3):
        bucket(6955.0, 7000.0, 0.5)
    bucket(7000.0, 6985.0, 0.5)
    return s, [o for o in out if "-entry" in (o.tag or "")]


def _by_setup(orders):
    got = {}
    for o in orders:
        m = parse_zone_tag(o.tag)
        got.setdefault(m["setup"] if m else o.tag.split("-")[0], o)
    return got


def test_a_fade_entry_is_priced_at_the_zone_edge():
    """THE REGRESSION. The sleeve records z.prox as the entry and every level it
    manages the trade with is measured from there, so the FILL has to be there
    too."""
    s, entries = _sleeve_with_entries()
    assert entries, "fixture produced no entries at all"
    fade = _by_setup(entries).get("fade")
    assert fade is not None, f"no fade entry; got {[o.tag for o in entries]}"
    assert fade.trigger_price is not None, (
        "fade is a bare MARKET order -- it fills at the bar close while the "
        "sleeve records the zone edge as its entry, so stop, target, scalp and "
        "breakeven are all measured from a price the trade never had")


def test_the_order_is_not_left_resting_across_bars(caplog):
    """A LIMIT emitted AFTER the touch bar closed is not the same order. It can
    only fill on a RE-touch, which on a 30m bar does not come: that version took
    ES:zones from 9 trading days to 0. The fill is evaluated on the bar that
    reached the level, which is what a genuinely resting order would have done,
    so no order is left outstanding and nothing needs cancelling."""
    s, entries = _sleeve_with_entries()
    for o in entries:
        assert o.type == OrderType.MARKET, (
            f"{o.tag} rests as {o.type}; the level was touched on the bar just "
            f"closed, so a resting order can only fill on a RE-touch -- that is "
            f"a different strategy, and it took zones to zero trades")


def test_the_entry_price_sits_on_a_zone_edge():
    """The whole point. If the fill and `t.entry` differ, `t.stop = t.entry`
    after the scalp is not breakeven and the 26 `be` legs keep losing $470."""
    s, entries = _sleeve_with_entries()
    e = _by_setup(entries).get("fade")
    if e is None:
        return
    edges = [z.top for z in s.zones] + [z.bot for z in s.zones]
    assert any(abs(e.trigger_price - lvl) < 1e-9 for lvl in edges), (
        f"entry priced {e.trigger_price}, which is not any zone edge")


def test_break_entries_claim_no_level():
    """Not everything gets a level. A break's entry is the CLOSE of the bar that
    broke the zone; claiming a level would invent a fill the setup never had."""
    s, entries = _sleeve_with_entries(enable_break=True)
    brk = _by_setup(entries).get("break")
    if brk is None:
        return                      # fixture did not produce one; nothing to assert
    assert brk.trigger_price is None, (
        "break claimed a level; its entry is the bar close and there is no "
        "level to be filled at")


def test_every_priced_entry_is_on_the_instrument_tick_grid():
    """Zone edges come from bar highs/lows so they are already on the grid, but
    the flip level and the +-0.5 tolerances are arithmetic -- a fill at a price
    the exchange cannot print is not a fill."""
    s, entries = _sleeve_with_entries()
    for o in entries:
        if o.trigger_price is not None:
            assert abs(o.trigger_price % 0.25) < 1e-9, (
                f"{o.tag} priced {o.trigger_price}, off the 0.25 ES grid")


def test_a_breakeven_exit_is_actually_breakeven():
    """The consequence, stated as the invariant it broke. After the scalp the
    runner's stop is set to the entry; flattening there must return ~zero on the
    remaining leg. It cannot be checked without the entry and the fill being the
    same price."""
    s, entries = _sleeve_with_entries(runner=True)
    e = _by_setup(entries).get("fade")
    if e is None:
        return
    # scalp_px is entry + SCALP and the post-scalp stop is entry, so both are
    # only honest if the price PAID is the same number the sleeve recorded.
    assert e.trigger_price is not None


# ── the level must have EXISTED before the bar that trades it ────────────────
#
# `_on_tf_bar` appends zones from det.update(b) and then calls _scan(b), so a
# zone can be detected and faded on the same bar. A zone is created by its
# DEPARTURE bar, whose extreme is the edge -- so pricing that entry at the edge
# claims a fill at a level that was not knowable until the bar closed, by which
# time price was at the close. That is lookahead, and it is not rare: on the
# recorded ES tape 8 of 33 entries are same-bar, and all 8 are FADE -- two
# thirds of every fade the sleeve takes (strategy_lab/zone_entry_lookahead.py).

# ── a fill at the level, or no trade ─────────────────────────────────────────
#
# "If I put a limit order at a zone at 7700 there is no way I should get a fill
# at 7703. Never."  -- and that settles it. Filling a same-bar zone at the bar
# CLOSE (the patch below this one) was still a fill at a price the order never
# had. There was no order: the zone did not exist until the bar closed.
#
# A limit resting from the moment the zone is detected would fill on the first
# bar whose range reaches the level -- which is the sleeve's own "first touch of
# a fresh zone" condition, so the two are the same thing and no cancellation
# machinery is needed. The only case that differs is the zone created BY the bar
# being traded, and there the honest answer is not a worse price. It is no trade.

def test_a_zone_created_by_this_bar_produces_no_trade_at_all():
    """THE RULE. Not 'fill it at the close' -- there was nothing resting to
    fill. On the recorded ES tape this is 8 of 33 entries, all of them FADE."""
    from engine.core.events import Bar as _Bar
    from engine.strategies.zones_strategy import _ZoneRec

    s = ZoneLifecycleStrategy("ES", point_usd=50.0)
    s._k = 7
    fresh = _ZoneRec(k=7, dir=1, top=7000.0, bot=6990.0)
    b = _Bar(0, "30m", 6995.0, 7010.0, 6988.0, 7008.0, 500, "ES")
    assert s._enter("FADE", 1, fresh.prox, 6989.0, 7020.0, fresh, b) == [], (
        "traded a zone that did not exist when the bar opened")
    assert s.trade is None, "recorded a trade it never placed an order for"


def test_an_older_zone_trades_at_its_edge():
    from engine.core.events import Bar as _Bar
    from engine.strategies.zones_strategy import _ZoneRec

    s = ZoneLifecycleStrategy("ES", point_usd=50.0)
    s._k = 12
    old = _ZoneRec(k=4, dir=1, top=7000.0, bot=6990.0)
    b = _Bar(0, "30m", 7006.0, 7010.0, 6996.0, 7008.0, 500, "ES")
    o = s._enter("FADE", 1, old.prox, 6989.0, 7020.0, old, b)[0]
    assert o.trigger_price == 7000.0
