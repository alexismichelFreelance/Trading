"""Clocks. The engine reads time only through this port, so timeouts and
session logic behave identically in replay (event-time) and live (wall-time)."""
from __future__ import annotations

import time


class EventClock:
    """Replay clock driven by the timestamp of each event (event-time).

    Tolerates repeated timestamps (many events share a 1-second bucket) but
    rejects regressions, which would signal an out-of-order feed.
    """
    __slots__ = ("_ts",)

    def __init__(self, start: int = 0) -> None:
        self._ts = start

    def now(self) -> int:
        return self._ts

    def set(self, ts: int) -> None:
        if ts < self._ts:
            raise ValueError(f"clock regression: {ts} < {self._ts}")
        self._ts = ts


class WallClock:
    """Live clock: the OS wall clock in epoch nanoseconds, UTC."""
    __slots__ = ()

    def now(self) -> int:
        return time.time_ns()

    def set(self, ts: int) -> None:  # noqa: D401 - wall clock is authoritative
        return None


__all__ = ["EventClock", "WallClock"]
