"""RiskSupervisor — monotonic _roll_day and the backward-day backfill guard.

Two distinct guarantees this file proves:

1. _roll_day is MONOTONIC: a catch-up / backlog event stamped on a PRIOR
   session date can never roll the supervisor BACKWARD. Rolling backward is
   how _eod_done and halted used to be reset, so a supervisor that had already
   flattened (or been killed) went back to work on stale counters.

2. An event stamped on a PRIOR day can never trigger an EOD flatten of the
   ACTIVE live session. This is the poisoning that a monotonic _roll_day alone
   does NOT prevent: during multi-lane warmup, one lane is already live on the
   current day while another lane is still replaying ITS backfill from the
   previous day. The live engine runs the supervisor on EVERY market event once
   any lane is live, so a warmer lane's backfill BAR stamped 16:00 ET
   yesterday lands inside the flatten window [15:58, 18:00) and — without the
   day guard — sets _eod_done=True for the wrong day. Then every live order on
   the real session is denied "post-EOD" and, worse, the real session never
   flattens. Both are wrong. The guard keys the EOD/halt decision on the
   CURRENT event's day equalling the tracked session day, so a prior-day
   backfill event is inert.
"""
import pandas as pd
import pytest

from engine.core.orders import Order
from engine.core.risk import RiskConfig, RiskSupervisor

BUY, SELL = 1, -1


def ts_et(hhmm: str, day: str = "2026-10-02") -> int:
    return int(pd.Timestamp(f"{day} {hhmm}", tz="America/New_York").value)


def test_monotonic_roll_day_ignores_backward_sessions():
    """Backward day transitions (catch-up) must not reset _eod_done or halted."""
    r = RiskSupervisor(RiskConfig())
    # 1. Advance to a day and set state
    r.on_market(ts_et("12:00", "2026-10-02"), 5000.0, [])
    r.halted = True
    r._eod_done = True
    assert r._day == "2026-10-02"

    # 2. Deliver a backward day event (catch-up from 2026-09-30)
    r.on_market(ts_et("12:00", "2026-09-30"), 5000.0, [])

    # 3. State must remain preserved
    assert r._day == "2026-10-02", "Backward day must not change _day"
    assert r.halted is True, "Backward day must not reset halted"
    assert r._eod_done is True, "Backward day must not reset _eod_done"


def test_monotonic_roll_day_still_rolls_forward():
    """A forward day transition must still reset the counters for the new day."""
    r = RiskSupervisor(RiskConfig(eod_flatten_et=(15, 58)))
    r.on_market(ts_et("12:00", "2026-10-01"), 5000.0, [])
    r._eod_done = True                      # old day already flattened
    assert r._day == "2026-10-01"

    # next-day event: counters reset, EOD re-arms for the new session
    r.on_market(ts_et("09:00", "2026-10-02"), 5000.0, [])
    assert r._day == "2026-10-02"
    assert r._eod_done is False, "forward roll must re-arm the new session's EOD"


def test_backward_day_backfill_does_not_poison_eod_done():
    """The multi-lane warmup poisoning: a PRIOR-day backfill BAR stamped inside
    the flatten window must NOT set _eod_done for the active live day."""
    r = RiskSupervisor(RiskConfig(eod_flatten_et=(15, 58)))
    books = [(1, "ES:onbreak", "ES", 10)]
    # The live session is 2026-10-02, tracked by a morning current-day event.
    r.on_market(ts_et("09:00", "2026-10-02"), 5000.0, books)
    assert r._day == "2026-10-02"
    assert r._eod_done is False

    # A warmer lane replays ITS backfill from 2026-10-01, stamped 16:00 ET —
    # inside the flatten window [15:58, 18:00). Without the day guard this
    # would flip _eod_done=True for the ACTIVE 10-02 session.
    out = r.on_market(ts_et("16:00", "2026-10-01"), 5000.0, books)
    assert out == [], "backward-day backfill must not emit flatten orders"
    assert r._eod_done is False, "prior-day backfill must not lock out the live session"
    assert r._day == "2026-10-02", "prior-day backfill must not move the day backward"

    # Live order on the real session must still be accepted (not denied post-EOD)
    o = Order("ES", BUY, 1, tag="entry")
    vetted = r.vet(1, "ES:onbreak", o, ts_et("10:00", "2026-10-02"), 0)
    assert vetted is not None, "live order must not be denied post-EOD"


def test_current_day_eod_still_fires():
    """The day guard must NOT suppress the real session's own flatten."""
    r = RiskSupervisor(RiskConfig(eod_flatten_et=(15, 58)))
    books = [(1, "ES:onbreak", "ES", 10)]
    r.on_market(ts_et("09:00", "2026-10-02"), 5000.0, books)
    # A CURRENT-day event inside the window must still flatten.
    out = r.on_market(ts_et("16:00", "2026-10-02"), 5000.0, books)
    assert len(out) == 1 and out[0][1].tag == "risk_eod"
    assert r._eod_done is True


def test_eod_flatten_window_bounds():
    """Verify that flatten only fires within the 15:58-18:00 ET window."""
    r = RiskSupervisor(RiskConfig(eod_flatten_et=(15, 58)))
    books = [(1, "s", "ES", 10)]

    assert r.on_market(ts_et("15:57"), 5000.0, books) == []

    out = r.on_market(ts_et("15:58"), 5000.0, books)
    assert len(out) == 1
    assert out[0][1].tag == "risk_eod"
    assert r._eod_done is True

    # Once _eod_done is True, no repeat for the same session
    assert r.on_market(ts_et("15:59"), 5000.0, books) == []


def test_backward_day_cannot_trigger_daily_loss_halt():
    """Prior-day backfill P&L must not trip the ACTIVE session's kill switch."""
    r = RiskSupervisor(RiskConfig(daily_loss_halt=-1000.0))
    books = [(1, "ES:onbreak", "ES", -10)]
    # track the live day with a morning event + a short position at 5000
    r.on_market(ts_et("09:00", "2026-10-02"), {"ES": 5000.0}, books)
    r.on_fill(1, "o1", -10, 5000.0, "ES")        # short 10 @ 5000
    assert r._avg[1] == 5000.0
    assert r._pos[1] == -10

    # A prior-day backfill event at a price that WOULD show a huge loss
    # (7000 on a 5000 short = -20000) — it must NOT arm the kill switch.
    out = r.on_market(ts_et("12:00", "2026-10-01"), {"ES": 7000.0}, books)
    assert r.halted is False, "backward-day event must not halt the live session"
    assert out == []

    # And a CURRENT-day event at the same lossy price still halts.
    out = r.on_market(ts_et("12:00", "2026-10-02"), {"ES": 7000.0}, books)
    assert r.halted is True, "current-day breach must still trip the kill switch"


def test_backward_day_ignored_after_halt_no_reset():
    """Regression: a backward-day event after a kill-switch halt must not
    resurrect trading (reset `halted`) on the live session."""
    r = RiskSupervisor(RiskConfig(daily_loss_halt=-1000.0))
    books = [(1, "ES:onbreak", "ES", 5)]
    r.on_market(ts_et("09:00", "2026-10-02"), {"ES": 5000.0}, books)
    r.halted = True

    r.on_market(ts_et("11:00", "2026-10-01"), {"ES": 5000.0}, books)
    assert r.halted is True, "backward-day event must not clear the halt"


def test_first_event_establishes_day():
    """A supervisor that sees its very first disabled/backfill event still
    tracks that day (no crash); a later forward event rolls it correctly."""
    r = RiskSupervisor(RiskConfig(eod_flatten_et=(15, 58)))
    books = [(1, "s", "ES", 5)]
    r.on_market(ts_et("16:00", "2026-10-01"), 5000.0, books)   # first event
    # This is the ONLY event so far: it is the day, and it is a real fatal
    # window — but since it is the *first* event the supervisor adopts it as
    # the session day and flattens that day's book (correct single-lane case).
    assert r._day == "2026-10-01"
    assert r._eod_done is True
    # A live next-day event rolls forward and re-arms.
    r.on_market(ts_et("09:00", "2026-10-02"), 5000.0, books)
    assert r._day == "2026-10-02"
    assert r._eod_done is False
