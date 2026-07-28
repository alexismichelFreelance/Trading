"""Time helpers. Internal time is epoch nanoseconds UTC; sessions are ET.

PERFORMANCE NOTE. These are on the hot path of every tick-driven sleeve, and a
pandas Timestamp + tz conversion costs 120-230us. Measured per call:

    et                 125.7us      et_session_date    226.6us
    et_minute_of_day   128.2us      is_rth             120.7us

At bar rates (~1,400 bars/session) that is irrelevant. At TICK rates it is not:
SweepFollowStrategy calls et_session_date AND et_minute_of_day on every trade,
which is ~355us of timezone arithmetic per tick before the sleeve does any work
at all. Profiled over a 540k-event ES session that made the six trade-driven
sleeves 88% of all engine compute, and put a 22-sleeve replay at 130-230 MINUTES
per session. In LIVE it is a latency risk on a burst, where the cost lands
precisely on the fastest bars.

So the derived values are CACHED PER SECOND. Every timestamp inside the same
second has the same ET date, minute-of-day and RTH flag, so the pandas work
happens once a second instead of once a tick. Results are identical by
construction -- this is memoisation of a pure function of `ns // NS`, not an
approximation, so replay parity is unaffected.
"""
from __future__ import annotations

import pandas as pd

NY = "America/New_York"
NS = 1_000_000_000

# RTH in ET minutes-of-day: 09:30 (=570) .. 16:00 (=960)
RTH_START_MIN = 570
RTH_END_MIN = 960

# second -> (session_date, minute_of_day). Bounded: a session is 86,400 seconds
# and the engine is restarted daily, so this cannot grow without limit in live.
# _MAX guards a long multi-day replay from unbounded growth.
_SEC_CACHE: dict[int, tuple[str, int]] = {}
_MAX = 400_000


def to_ns(ts) -> int:
    """Coerce a QuestDB/ISO/pandas timestamp to epoch ns (UTC)."""
    return int(pd.Timestamp(ts).tz_localize("UTC").value) if pd.Timestamp(ts).tzinfo is None \
        else int(pd.Timestamp(ts).tz_convert("UTC").value)


def ns_to_utc(ns: int) -> pd.Timestamp:
    return pd.Timestamp(ns, unit="ns", tz="UTC")


def et(ns: int) -> pd.Timestamp:
    """Full ET Timestamp. NOT cached -- callers that only need the session date
    or the minute should use the helpers below, which are."""
    return ns_to_utc(ns).tz_convert(NY)


def _sec_parts(ns: int) -> tuple[str, int]:
    sec = ns // NS
    hit = _SEC_CACHE.get(sec)
    if hit is not None:
        return hit
    t = et(ns)
    parts = (t.strftime("%Y-%m-%d"), t.hour * 60 + t.minute)
    if len(_SEC_CACHE) >= _MAX:
        _SEC_CACHE.clear()
    _SEC_CACHE[sec] = parts
    return parts


def et_session_date(ns: int) -> str:
    """ET calendar date (the trading session key), 'YYYY-MM-DD'."""
    return _sec_parts(ns)[0]


def et_minute_of_day(ns: int) -> int:
    return _sec_parts(ns)[1]


def utc_hour(ns: int) -> str:
    """UTC hour key 'YYYY-MM-DD HH:00' (matches the frozen regime-state keys)."""
    return ns_to_utc(ns).strftime("%Y-%m-%d %H:00")


def is_rth(ns: int) -> bool:
    m = _sec_parts(ns)[1]
    return RTH_START_MIN <= m < RTH_END_MIN


__all__ = [
    "NY", "NS", "RTH_START_MIN", "RTH_END_MIN",
    "to_ns", "ns_to_utc", "et", "et_session_date", "et_minute_of_day", "is_rth",
]
