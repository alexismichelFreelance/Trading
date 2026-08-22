"""Was the move into a range break actually TRADED, or did price fall through air?

A breakout is only information if the move that produced it consumed liquidity.
When sellers hammer bids, size changes hands and the new price has been paid for
-- somebody now owns it and has a reason to defend it. When the bids are simply
CANCELLED, price slides through an empty book, nothing changed hands, and there
is nobody with a position to defend. Those snap back.

The two cases are distinguishable while they happen, because every trade carries
its aggressor: price falling with net SELLING is supply, price falling with net
BUYING is air.

MEASURED, 45 ORB breaks over 24 sessions with per-second flow (ES + NQ),
strategy_lab/vacuum_break.py, 1 contract gross:

    real trading   23 trades   +19,220$   avg  +836$   won 65%
    fell on air    22 trades    +3,128$   avg  +142$   won 50%

and the same ordering on each instrument separately (ES +5,550 vs +3,362;
NQ +13,670 vs -235). The four worst losses in the sample are all air breaks,
including 2026-08-19 NQ -2,265$, where NQ fell 350 points -- a large part of it
while buyers were the aggressive side -- and opendrive shorted 30 points off the
low and bled until the session flat.

THE LEG IS STRUCTURAL, NOT A CLOCK. The move into a downside break runs from the
session high that preceded it, however long that took. A fixed lookback was tried
first and is wrong: at 20 minutes it misclassified 2026-08-19 NQ -- the very move
this came from -- because that window caught only the tail of the decline, where
selling was real. The pivot is the move; a constant is a guess about the move.

THE THRESHOLD IS A TRAILING MEDIAN, NOT A CONSTANT. `vac` is a ratio, but its
typical level is a property of the instrument and the period, and a number fitted
to these 45 breaks is a number fitted to these 45 breaks. So the gate compares
today against the median of PRIOR sessions only, and fails OPEN until it has
enough of them -- an absent history must never silently stop a sleeve trading.
"""
from __future__ import annotations

from collections import deque

from ..core.timeutil import et_minute_of_day, et_session_date


class BreakQuality:
    """Per-session accumulator: one (last price, signed aggressor volume) pair
    per ET minute, from the raw trade stream. Causal -- it only ever sees trades
    that have already printed."""

    def __init__(self, hist: int = 20, min_sessions: int = 5,
                 start_min: int = 9 * 60 + 30) -> None:
        self.hist = deque(maxlen=hist)     # prior sessions' vac values
        self.min_sessions = min_sessions
        self.start_min = start_min
        self._day: str | None = None
        self._min: list[list] = []         # [minute_of_day, last_px, delta]

    # ── accumulation ─────────────────────────────────────────────────────
    def on_trade(self, ts: int, price: float, size: int, aggressor: int) -> None:
        day = et_session_date(ts)
        if day != self._day:
            self.roll_session()
            self._day = day
        m = et_minute_of_day(ts)
        if m < self.start_min:
            return                          # pre-open prints are not the move
        if self._min and self._min[-1][0] == m:
            row = self._min[-1]
            row[1] = float(price)
            row[2] += int(size) * int(aggressor)
        else:
            self._min.append([m, float(price), int(size) * int(aggressor)])

    def roll_session(self) -> None:
        """Close the day out: bank its vac (if any) so tomorrow has a reference,
        and clear. Safe to call more than once."""
        v = self._vac_all()
        if v is not None:
            self.hist.append(v)
        self._min = []

    # ── the question ─────────────────────────────────────────────────────
    def vac(self, side: int) -> float | None:
        """Share of the move INTO the break that happened on opposing delta.

        0.0 = every inch was paid for by aggressors on the break's own side.
        1.0 = the whole move was air. None = not enough of a move to judge.

        `side` is the break's direction: +1 upside, -1 downside."""
        if len(self._min) < 4:
            return None
        px = [r[1] for r in self._min]
        # structural pivot: the extreme the move ran FROM
        piv = px.index(max(px)) if side < 0 else px.index(min(px))
        leg = self._min[piv:]
        if len(leg) < 3:
            return None
        moved = wrong = 0.0
        for a, b in zip(leg, leg[1:]):
            d = b[1] - a[1]
            if d == 0.0 or (d > 0) != (side > 0):
                continue                    # only movement in the break's direction
            moved += abs(d)
            if (b[2] > 0) != (side > 0):    # delta pointed the other way
                wrong += abs(d)
        return (wrong / moved) if moved > 0 else None

    def _vac_all(self) -> float | None:
        """The day's vac read on whichever direction the session actually ran.
        Used only to feed the trailing median, never to gate."""
        if len(self._min) < 4:
            return None
        px = [r[1] for r in self._min]
        side = -1 if px.index(max(px)) < px.index(min(px)) else 1
        return self.vac(side)

    def threshold(self) -> float | None:
        """Median vac of prior sessions, or None while still warming up."""
        if len(self.hist) < self.min_sessions:
            return None
        s = sorted(self.hist)
        n = len(s)
        return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])

    def ok(self, side: int) -> bool:
        """True = the break was traded into; take it. FAILS OPEN: no history, no
        measurable move, or a flat session all allow the entry, because a missing
        measurement is not evidence of a bad break."""
        th = self.threshold()
        if th is None:
            return True
        v = self.vac(side)
        return True if v is None else v <= th


__all__ = ["BreakQuality"]
