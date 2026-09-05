"""OvernightFadeStrategy: fades the 18:00->09:30 move at the open, exits at a
target that is a FRACTION OF THAT MOVE, stops at a fraction of the overnight
RANGE, takes one shot per session, and stands down on a partial night.

The session key is the sharp edge here: et_session_date is the ET CALENDAR date
and does NOT roll at the Globex open, so a 20:00 Monday bar and the 09:30
Tuesday open it precedes carry different dates. If the sleeve keyed on it the
overnight window would be discarded every single day and the sleeve would never
trade. test_session_key_rolls_at_globex pins that.
"""
import pandas as pd

from engine.core.events import Bar, PositionUpdate
from engine.strategies.overnight_fade import OvernightFadeStrategy


def _b(et_str, o, h, l, c, v=1000):
    ts = int(pd.Timestamp(et_str, tz="America/New_York").value)
    return Bar(ts, "1m", o, h, l, c, v, "ES")


def _drive(strat, bars):
    orders, pos = [], 0
    for b in bars:
        for o in strat.on_bar(b):
            orders.append(o)
            pos += o.side * o.qty
            strat.on_position(PositionUpdate(b.ts, strat.symbol, pos, 0.0))
    return orders, pos


def _night(day_prev="2026-07-23", day="2026-07-24", start=6000.0, end=6020.0,
           hi=6025.0, lo=5995.0, n=300):
    """A night that RISES `start`->`end` in n bars, ranging [lo, hi].

    Bars run from 18:00 the previous evening, so they exercise the calendar-date
    roll. Range extremes are planted on the first bar."""
    t0 = pd.Timestamp(f"{day_prev} 18:00", tz="America/New_York")
    bars = []
    for i in range(n):
        px = start + (end - start) * i / max(n - 1, 1)
        h, l = (hi, lo) if i == 0 else (px + 0.5, px - 0.5)
        ts = t0 + pd.Timedelta(minutes=i)
        bars.append(Bar(int(ts.value), "1m", px, max(h, px), min(l, px), px, 1000, "ES"))
    return bars


def test_fades_an_up_night_and_takes_the_retracement_target():
    # night +20 pts (6000 -> 6020), range 30 (5995..6025)
    # entry at the 09:30 close 6020 -> SHORT, target 75% of 20 = 15 -> 6005
    bars = _night() + [
        _b("2026-07-24 09:30", 6020, 6021, 6019, 6020),   # entry bar
        _b("2026-07-24 10:00", 6018, 6019, 6010, 6012),   # drifting down, no touch
        _b("2026-07-24 10:30", 6012, 6013, 6004, 6006),   # low 6004 <= 6005: target
    ]
    s = OvernightFadeStrategy("ES")
    orders, pos = _drive(s, bars)
    entry = next(o for o in orders if o.tag == "entry-onfade")
    assert entry.side == -1                       # faded the up night
    exit_ = next(o for o in orders if o.tag == "onfade-target")
    assert exit_.side == 1 and pos == 0
    assert exit_.trigger_price == 6005.0          # the LEVEL, not the bar close


def test_stop_is_a_fraction_of_the_overnight_range():
    # range 30 -> stop 0.75 * 30 = 22.5 above a short entered at 6020 -> 6042.5
    bars = _night() + [
        _b("2026-07-24 09:30", 6020, 6021, 6019, 6020),
        _b("2026-07-24 10:00", 6030, 6043, 6029, 6041),   # high 6043 >= 6042.5
    ]
    s = OvernightFadeStrategy("ES")
    orders, pos = _drive(s, bars)
    stop = next(o for o in orders if o.tag == "onfade-stop")
    assert stop.trigger_price == 6042.5 and pos == 0


def test_stop_wins_when_a_bar_spans_both_barriers():
    # A bar that reaches the target AND the stop is booked as a LOSS: the
    # strategy cannot see the order the two were touched in, so it takes the
    # bad one. Optimism here is what makes a backtest unreproducible live.
    bars = _night() + [
        _b("2026-07-24 09:30", 6020, 6021, 6019, 6020),
        _b("2026-07-24 10:00", 6020, 6050, 6000, 6020),
    ]
    s = OvernightFadeStrategy("ES")
    orders, _ = _drive(s, bars)
    assert any(o.tag == "onfade-stop" for o in orders)
    assert not any(o.tag == "onfade-target" for o in orders)


def test_fades_a_down_night_long():
    bars = _night(start=6020.0, end=6000.0) + [
        _b("2026-07-24 09:30", 6000, 6001, 5999, 6000),
    ]
    s = OvernightFadeStrategy("ES")
    orders, pos = _drive(s, bars)
    assert next(o for o in orders if o.tag == "entry-onfade").side == 1
    assert pos == 1


def test_partial_night_stands_down():
    bars = _night(n=50) + [_b("2026-07-24 09:30", 6020, 6021, 6019, 6020)]
    s = OvernightFadeStrategy("ES")
    orders, pos = _drive(s, bars)
    assert orders == [] and pos == 0


def test_one_shot_only_no_late_entry():
    # the 09:30 bar stands the sleeve down (short night); it must NOT enter later
    bars = _night(n=50) + [
        _b("2026-07-24 09:30", 6020, 6021, 6019, 6020),
        _b("2026-07-24 11:00", 6020, 6021, 6019, 6020),
        _b("2026-07-24 14:00", 6020, 6021, 6019, 6020),
    ]
    s = OvernightFadeStrategy("ES")
    orders, _ = _drive(s, bars)
    assert not any(o.tag == "entry-onfade" for o in orders)


def test_flat_at_the_close_if_neither_barrier_is_touched():
    bars = _night() + [
        _b("2026-07-24 09:30", 6020, 6021, 6019, 6020),
        _b("2026-07-24 12:00", 6019, 6020, 6018, 6019),
        _b("2026-07-24 15:59", 6019, 6020, 6018, 6019),
    ]
    s = OvernightFadeStrategy("ES")
    orders, pos = _drive(s, bars)
    moc = next(o for o in orders if o.tag == "onfade-moc")
    assert moc.trigger_price is None              # a genuine market exit
    assert pos == 0


def test_session_key_rolls_at_globex():
    ts_eve = int(pd.Timestamp("2026-07-23 20:00", tz="America/New_York").value)
    ts_open = int(pd.Timestamp("2026-07-24 09:30", tz="America/New_York").value)
    ts_pre = int(pd.Timestamp("2026-07-23 17:00", tz="America/New_York").value)
    k = OvernightFadeStrategy.session_key
    assert k(ts_eve) == k(ts_open) == "2026-07-24"
    assert k(ts_pre) == "2026-07-23"              # 16:00-18:00 belongs to the day


def test_holiday_early_close_carries_and_is_flattened_at_the_reopen():
    """KNOWN GAP, pinned deliberately rather than left to be discovered live.

    On an exchange early close (13:00 ET: Juneteenth, July 3rd, the day after
    Thanksgiving, Christmas Eve) there is no bar at 15:59, so the EOD flat never
    fires and the position rides to the next session's first bar. The engine has
    no exchange calendar, and no rule evaluated on the tape can help: the market
    is shut, so there is nothing to exit into. 2 of 67 sessions in the 2026
    smoke replay, both real early closes, averaging -$541.

    The exit is at the REOPEN price, which is the honest one -- the research
    script booked the last RTH close on those days and was therefore slightly
    optimistic. Fixing this needs a holiday calendar, not a change here."""
    bars = _night() + [
        _b("2026-07-24 09:30", 6020, 6021, 6019, 6020),      # short 6020
        _b("2026-07-24 13:00", 6019, 6020, 6018, 6019),      # early close, last bar
        _b("2026-07-26 18:00", 6040, 6041, 6039, 6040),      # Sunday reopen, gapped
    ]
    s = OvernightFadeStrategy("ES")
    orders, pos = _drive(s, bars)
    assert not any(o.tag == "onfade-moc" for o in orders)    # never fired
    flat = next(o for o in orders if o.tag == "safety-flat")
    assert flat.side == 1 and flat.trigger_price is None     # fills at the reopen
    assert pos == 0
