"""Time helpers. Internal time is epoch nanoseconds UTC; sessions are ET."""
from __future__ import annotations

import pandas as pd

NY = "America/New_York"
NS = 1_000_000_000

# RTH in ET minutes-of-day: 09:30 (=570) .. 16:00 (=960)
RTH_START_MIN = 570
RTH_END_MIN = 960


def to_ns(ts) -> int:
    """Coerce a QuestDB/ISO/pandas timestamp to epoch ns (UTC)."""
    return int(pd.Timestamp(ts).tz_localize("UTC").value) if pd.Timestamp(ts).tzinfo is None \
        else int(pd.Timestamp(ts).tz_convert("UTC").value)


def ns_to_utc(ns: int) -> pd.Timestamp:
    return pd.Timestamp(ns, unit="ns", tz="UTC")


def et(ns: int) -> pd.Timestamp:
    return ns_to_utc(ns).tz_convert(NY)


def et_session_date(ns: int) -> str:
    """ET calendar date (the trading session key), 'YYYY-MM-DD'."""
    return et(ns).strftime("%Y-%m-%d")


def et_minute_of_day(ns: int) -> int:
    t = et(ns)
    return t.hour * 60 + t.minute


def utc_hour(ns: int) -> str:
    """UTC hour key 'YYYY-MM-DD HH:00' (matches the frozen regime-state keys)."""
    return ns_to_utc(ns).strftime("%Y-%m-%d %H:00")


def is_rth(ns: int) -> bool:
    m = et_minute_of_day(ns)
    return RTH_START_MIN <= m < RTH_END_MIN


__all__ = [
    "NY", "NS", "RTH_START_MIN", "RTH_END_MIN",
    "to_ns", "ns_to_utc", "et", "et_session_date", "et_minute_of_day", "is_rth",
]
