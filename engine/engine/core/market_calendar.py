"""Exchange calendar — when the session actually ends, not when we assume it does.

2026-09-07, Labor Day. ES opened normally and CLOSED AT 13:00 ET. Every sleeve
flattens at a hardcoded 15:59 and the RiskSupervisor's backstop at 15:58, so
nothing fired, and a live `onfade` long entered at 09:40 rode through the close,
through the 18:00 Globex reopen, and into the next session. The engine had no
idea the day was short.

It is not fixable from the tape. A rule that watches for the market going quiet
learns the session ended only AFTER it ended, and by then there is nothing to
exit into -- the market is shut. The close has to be known in advance, which
means a calendar.

WHAT THE TAPE DOES SAY. Every session in claude_bars_live (1,150 with an RTH,
2019-12 to 2026-09) has its last RTH bar at 15:59 except three:

    2026-06-19 Fri  13:00     Juneteenth
    2026-07-03 Fri  13:00     Independence Day (observed)
    2026-09-07 Mon  12:59     Labor Day

So the RULE is confirmed -- US market holidays trade a normal open and close at
13:00 ET -- on the only three instances the data contains. That is thin, which
is why the dates below are COMPUTED FROM RULES (third Monday of January, last
Monday of May, ...) rather than typed out year by year: a rule cannot silently
expire at the end of a hardcoded list, and tests/test_market_calendar.py checks
it against those three observed closes.

ONE TIME FOR EVERY HOLIDAY, DELIBERATELY. Some of these are really 13:15 (the
day after Thanksgiving, Christmas Eve) and some are full closures (Good Friday,
Christmas, New Year's Day). This treats all of them as a 13:00 ET close, because
the two errors are not symmetric: being flat 15 minutes early costs a little
edge on two days a year, and being flat too LATE is what left a live position
open across a closed market. When unsure, close earlier.

The calendar is US/ET and applies to the CME equity index futures this engine
trades (ES, NQ). It says nothing about other products.
"""
from __future__ import annotations

import datetime as _dt
from functools import lru_cache

from .timeutil import et_minute_of_day, et_session_date

RTH_CLOSE_MIN = 16 * 60          # 16:00 ET — the normal cash close
EARLY_CLOSE_MIN = 13 * 60        # 13:00 ET — every holiday session, see above
FLAT_LEAD_MIN = 1                # be flat one minute before the close
LOCKOUT_LEAD_MIN = 15            # no new exposure this long before the close


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> _dt.date:
    """The n-th `weekday` (Mon=0) of a month; n=-1 means the last one.

    The `n=-1` branch used to walk forward from the 28th while the next week
    stayed inside the month, which drops the last week whenever the month ends
    between the 29th and the 31st: it made the last Monday of May 2027 the 24th
    instead of the 31st. Counting back from the actual last day cannot do that."""
    if n > 0:
        d = _dt.date(year, month, 1)
        d += _dt.timedelta(days=(weekday - d.weekday()) % 7)
        return d + _dt.timedelta(weeks=n - 1)
    import calendar as _cal
    d = _dt.date(year, month, _cal.monthrange(year, month)[1])
    return d - _dt.timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> _dt.date:
    """Anonymous Gregorian algorithm. Good Friday is Easter minus two days."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, 0
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return _dt.date(year, month, day)


def _observed(d: _dt.date) -> _dt.date:
    """A holiday on a Saturday is kept on the Friday; on a Sunday, the Monday."""
    if d.weekday() == 5:
        return d - _dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + _dt.timedelta(days=1)
    return d


@lru_cache(maxsize=32)
def holidays(year: int) -> frozenset:
    """Every US market holiday in `year`, as 'YYYY-MM-DD'. Computed, never typed."""
    ds = [
        _observed(_dt.date(year, 1, 1)),                 # New Year's Day
        _nth_weekday(year, 1, 0, 3),                     # MLK, 3rd Monday Jan
        _nth_weekday(year, 2, 0, 3),                     # Presidents' Day
        _easter(year) - _dt.timedelta(days=2),           # Good Friday
        _nth_weekday(year, 5, 0, -1),                    # Memorial Day, last Mon
        _observed(_dt.date(year, 6, 19)),                # Juneteenth
        _observed(_dt.date(year, 7, 4)),                 # Independence Day
        _nth_weekday(year, 9, 0, 1),                     # Labor Day, 1st Mon Sep
        _nth_weekday(year, 11, 3, 4),                    # Thanksgiving, 4th Thu
        _nth_weekday(year, 11, 3, 4) + _dt.timedelta(1),  # the Friday after
        _dt.date(year, 12, 24),                          # Christmas Eve
        _observed(_dt.date(year, 12, 25)),               # Christmas
    ]
    # 2026-07-03 is in the tape as an early close and July 4th is a Saturday, so
    # the OBSERVED date is what trades short -- which _observed() already gives.
    return frozenset(d.isoformat() for d in ds if d.weekday() < 5)


def close_min(day: str) -> int:
    """RTH close, in ET minutes, for the session dated `day` ('YYYY-MM-DD')."""
    try:
        year = int(day[:4])
    except (ValueError, TypeError):
        return RTH_CLOSE_MIN
    return EARLY_CLOSE_MIN if day in holidays(year) else RTH_CLOSE_MIN


def is_holiday(day: str) -> bool:
    try:
        return day in holidays(int(day[:4]))
    except (ValueError, TypeError):
        return False


def flat_min(day: str) -> int:
    """The minute an intraday sleeve must be flat by. 15:59 normally, 12:59 on a
    holiday."""
    return close_min(day) - FLAT_LEAD_MIN


def lockout_min(day: str) -> int:
    """No new exposure from here. 15:45 normally, 12:45 on a holiday."""
    return close_min(day) - LOCKOUT_LEAD_MIN


def flat_min_for(ts: int) -> int:
    """flat_min for the session a nanosecond timestamp falls in."""
    return flat_min(et_session_date(ts))


def past_flat(ts: int) -> bool:
    """True once this session's flatten minute has arrived. THE call site for
    every sleeve's end-of-day exit -- replaces `m >= 15*60+59`."""
    return et_minute_of_day(ts) >= flat_min_for(ts)


__all__ = ["RTH_CLOSE_MIN", "EARLY_CLOSE_MIN", "close_min", "flat_min",
           "lockout_min", "flat_min_for", "past_flat", "is_holiday", "holidays"]
