"""How much of a normal day has already happened?

The quantity a discretionary trader means by "considering the ranges we are
currently in", and the one nothing on the roster computed. On 2026-08-21, at the
minute ES printed its high of the day:

    ES  range_used 0.86     NQ  range_used 1.06

The day had already produced 86% of a normal session's range on ES and MORE than
a full one on NQ. Holding past that is holding for the last sliver of a typical
opportunity against the full downside of a reversal -- a bad bet on its own
terms, and knowable without predicting anything.

WHAT IT IS. The session's high-low so far, divided by the median RTH range of
the previous `hist` sessions. 1.0 means today has already covered a typical
day's ground.

WHY THE MEDIAN OF PRIOR SESSIONS AND NOT A CONSTANT. Range regimes move: ES ran
a 43-point median in August 2026 and NQ 252. A fixed points threshold is a
statement about one regime and silently becomes wrong in the next. The reference
is rebuilt from the trailing window and uses ONLY completed prior sessions, so
today can never appear in its own denominator.

FAILS OPEN. Before `min_sessions` prior days exist, `used()` returns None and
every consumer must treat that as "no opinion". A feature that has not warmed up
must never be the reason a sleeve stands down.
"""
from __future__ import annotations

from collections import deque

from ..core.timeutil import et_minute_of_day, et_session_date

RTH_OPEN = 9 * 60 + 30
RTH_CLOSE = 16 * 60


class DayRange:
    def __init__(self, hist: int = 10, min_sessions: int = 3,
                 push_win_s: int = 600) -> None:
        self.hist: deque = deque(maxlen=hist)
        self.min_sessions = min_sessions
        self.push_win_s = push_win_s
        self._day: str | None = None
        self.hi: float | None = None
        self.lo: float | None = None
        self.last: float | None = None
        self._buf: deque = deque()          # (ts_ns, price) over the push window
        # counter-move tracking, for `legs`
        self._last_ts = 0                    # for idempotent feeding
        self._up_from: float | None = None   # running extreme in the day's direction
        self._legs = 0
        self._in_leg = False

    # ── accumulation ─────────────────────────────────────────────────────
    def note(self, ts: int, price: float) -> None:
        """Feed every price. RTH only -- the overnight session has its own,
        much wider, range and mixing them makes the ruler meaningless.

        IDEMPOTENT ON TIMESTAMP. One instance is shared by every strategy that
        wants it (see tools/run_live.attach_day_range), and each feeds it from
        its own dispatch, so the same event arrives once per sleeve. Anything
        not strictly newer than the last accepted timestamp is ignored -- the
        first sleeve to see an event updates the ruler and the rest are no-ops.
        Without this the range would be correct but `legs` and the push buffer
        would count each event N times. Only an EXACT timestamp repeat is
        dropped: rejecting anything older would silently discard out-of-order
        data too, and deciding what to do about that belongs to the engine's
        stale-event guard, not to an indicator."""
        if ts == self._last_ts:
            return                      # same event, a second sleeve
        self._last_ts = ts
        day = et_session_date(ts)
        if day != self._day:
            self.roll_session()
            self._day = day
        m = et_minute_of_day(ts)
        if m < RTH_OPEN or m >= RTH_CLOSE:
            return
        p = float(price)
        self.hi = p if self.hi is None else max(self.hi, p)
        self.lo = p if self.lo is None else min(self.lo, p)
        self.last = p
        self._count_leg(p)
        self._buf.append((ts, p))
        cut = ts - self.push_win_s * 1_000_000_000
        while self._buf and self._buf[0][0] < cut:
            self._buf.popleft()

    def roll_session(self) -> None:
        """Bank the finished session's range and start clean. Idempotent."""
        if self.hi is not None and self.lo is not None and self.hi > self.lo:
            self.hist.append(self.hi - self.lo)
        self.hi = self.lo = self.last = None
        self._buf.clear()
        self._up_from = None
        self._legs = 0
        self._in_leg = False

    # ── the questions ────────────────────────────────────────────────────
    def typical(self) -> float | None:
        """Median RTH range of the prior sessions, or None while warming up."""
        if len(self.hist) < self.min_sessions:
            return None
        s = sorted(self.hist)
        n = len(s)
        return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])

    def today(self) -> float | None:
        if self.hi is None or self.lo is None:
            return None
        return self.hi - self.lo

    def used(self) -> float | None:
        """Fraction of a typical session's range consumed so far. None = no
        opinion yet, which callers must treat as permission, not refusal."""
        t, d = self.typical(), self.today()
        if t is None or d is None or t <= 0:
            return None
        return d / t

    def extended(self, level: float) -> bool:
        """True only when we KNOW the day is at least `level` of a normal range.
        Unknown is False, so this can never manufacture a signal from ignorance."""
        u = self.used()
        return u is not None and u >= level


    # ── arrival speed ────────────────────────────────────────────────────
    def push(self, my_dir: int) -> float | None:
        """Travel over the last `push_win_s` seconds in `my_dir`, as a fraction
        of a typical session's range. None until the ruler and the buffer are
        both ready."""
        t = self.typical()
        if t is None or t <= 0 or self.last is None or len(self._buf) < 2:
            return None
        return ((self.last - self._buf[0][1]) * (1 if my_dir > 0 else -1)) / t

    def at_extreme(self, my_dir: int, tol: float = 0.10) -> bool:
        """Is price AT the session extreme it has been running toward? `tol` is
        a fraction of TODAY's range, so it scales with the day rather than
        asserting a points distance."""
        d = self.today()
        if d is None or d <= 0 or self.last is None:
            return False
        ext = self.hi if my_dir > 0 else self.lo
        return ext is not None and abs(ext - self.last) <= tol * d

    def fast_extreme(self, my_dir: int, level: float) -> bool:
        """Price is at a session extreme AND got there fast.

        Measured over 69 extremes of 2025 Databento MBO and 48 of the 2026 live
        record: an extreme reached quickly reverses about TWICE as hard as one
        ground into slowly, in all four cells (2025/2026 x tops/bottoms). The
        high-push tercile begins near 0.15 of a daily range travelled in ten
        minutes -- 2025 tops 0.111 / bottoms 0.197, 2026 tops 0.159 / bottoms
        0.174 -- so the boundary is bracketed by the roster, not fitted.

        This is the only reversal feature that survived out-of-sample testing,
        and the only one computable from price alone -- conviction inverted
        between feeds, and everything order-level needs order ids the live feed
        does not carry. Unknown returns False: it can never invent a signal."""
        p = self.push(my_dir)
        return p is not None and p >= level and self.at_extreme(my_dir)


    # ── counter-moves ────────────────────────────────────────────────────
    def _count_leg(self, p: float) -> None:
        """Count pullbacks deeper than 25% of the range built so far.

        A day is 'in a leg' once it has retraced past that fraction from its
        running extreme; the leg ends when a new extreme is made. `legs` is the
        number of completed counter-moves."""
        if self.hi is None or self.lo is None:
            return
        rng = self.hi - self.lo
        if rng <= 0:
            return
        up = (self.hi - p) <= (p - self.lo)      # nearer the high == an up day
        draw = (self.hi - p) if up else (p - self.lo)
        if draw > 0.25 * rng:
            self._in_leg = True
        elif self._in_leg and draw <= 0.05 * rng:
            self._legs += 1                       # recovered to the extreme
            self._in_leg = False

    def legs(self) -> int:
        """Completed counter-moves so far today."""
        return self._legs

    def one_way(self) -> bool:
        """True when the day has built its range WITHOUT a single meaningful
        pullback -- the shape that keeps expanding.

        Measured at the moment a session first reaches 0.80 of a typical range
        (strategy_lab/range_break.py), over 44 sessions of 2025 Databento MBO and
        27 of the 2026 live record:

            legs == 0   extra range afterwards +0.71 / +0.58   expands 60% / 56%
            legs >= 1   extra range afterwards +0.47 / +0.35   expands 41% / 39%

        A ~1.4x tilt, consistent in direction across both datasets -- not a
        switch. It is worth acting on only because the errors are asymmetric:
        scaling out early on a day that keeps running costs half the remaining
        move (+0.5 to +1.24 of a range on the four worst 2026 cases), while
        failing to scale on a finished day costs only a bounded give-back.

        The other candidates were tested and rejected: piercing the prior day's
        high or low carried NO information (gap 0.05 / 0.24, inconsistent sign),
        the overnight gap died between datasets (0.26 -> 0.01), and one-way
        travel was flat (0.13 / 0.05). Volume and pace did replicate but are
        collinear with how early the trigger fires."""
        return self._legs == 0


__all__ = ["DayRange"]
