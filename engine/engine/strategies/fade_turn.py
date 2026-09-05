"""FadeTurnStrategy — the user's own intraday entry, encoded from measurement.

Derived from the entry-context study (155 of his intraday entries vs every other
minute of the same sessions, scored in BOTH directions and matched on symbol,
direction and clock time). Against that control his entries sat at:

    30-min move signed to the trade   -1.60 ATR   (control  0.00)
    60-min move signed                -1.63       (control  0.00)
    15-min move signed                -0.72       (control  0.00)
    LAST 5 min signed                 +0.22       (control  0.00)  <- sign flips

i.e. he fades a 30-60 minute move, but only enters once the last few minutes
have already turned his way. Every one of those held the same sign across ES,
NQ, longs, shorts, winners, losers and both halves of the sample.

THE CONDITIONS HERE ARE SIGN-ONLY -- "the 30-min move went against the trade"
and "the last 5 minutes turned back" -- with no fitted magnitude anywhere. The
study's medians say WHERE the mass sits; turning them into thresholds would be
fitting them. The one number that IS a threshold is the clock window, and that
is the claim under test: outcomes split hard on it.

        08-11 ET, shallow fade   n=43  72% win  median $4,663
        08-11 ET, deep fade      n=36  69% win  median $3,110
        outside,  shallow fade   n=35  69% win  median $2,245
        outside,  deep fade      n=41  37% win  median  -$925   <- the only loser

So `fadeturn` runs 08:00-11:00 ET and `fadeturn_all` drops the window. If the
window is real, the pair separates; if it is not, this sleeve dies and says so.

Exits are NOT tuned, and there are exactly two, both chosen a priori:

  stop_mode="structural"  stop at the extreme of the move being faded.
      Sounds principled and is NOT: fading a decline means that low sits a few
      points beneath the entry, so it is a very tight stop by construction. It
      stopped out 79% of trades. Kept as the control that shows this.
  stop_mode="none"        no stop; hold to the time stop, like the user.
      He does not use a tight stop -- he holds a median 84 minutes and ADDS
      against the move. This is the exit that matches the behaviour the entry
      was measured from.

Exits change WHICH TRADES EXIST, so both are replayed in full rather than
re-scored on one ledger, and nothing beyond these two is swept.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ..core.events import Bar
from ..core.orders import Order
from ..core.timeutil import et_minute_of_day, et_session_date
from .base import BaseStrategy, level_fill
from .sizing import position_size

WIN_OPEN, WIN_CLOSE = 8 * 60, 11 * 60      # 08:00-11:00 ET, the measured window
LOOKBACK = 30                              # the faded move, in minutes
TURN = 5                                   # the turn-back window, in minutes
ATR_WIN = 30
HOLD = 84                                  # his median intraday hold, minutes
MOC_MIN = 959
COOL = 15
RISK = 1000.0
SIZE_CAP = 5


@dataclass
class _Pos:
    dir: int
    entry: float
    stop: float
    size: int
    age: int = 0


class FadeTurnStrategy(BaseStrategy):
    def __init__(self, symbol: str, point_usd: float = 50.0,
                 use_window: bool = True, stop_mode: str = "none") -> None:
        if stop_mode not in ("structural", "none"):
            raise ValueError(f"stop_mode must be structural|none, got {stop_mode!r}")
        self.symbol = symbol
        self.point_usd = point_usd
        self.use_window = use_window
        self.stop_mode = stop_mode
        self.pos = 0
        self.trade: _Pos | None = None
        self._day: str | None = None
        self._reset_session()

    def _reset_session(self) -> None:
        self.c: deque[float] = deque(maxlen=LOOKBACK + 2)
        self.lo: deque[float] = deque(maxlen=LOOKBACK + 2)
        self.hi: deque[float] = deque(maxlen=LOOKBACK + 2)
        self.tr: deque[float] = deque(maxlen=ATR_WIN)
        self._cool = 0
        self._armed_dir = 0          # direction whose turn-back was already true

    def on_position(self, p) -> None:
        self.pos = p.qty

    def reset_for_live(self) -> None:
        self.trade = None
        self.pos = 0

    def on_bar(self, bar: Bar) -> list[Order]:
        day = et_session_date(bar.ts)
        if day != self._day:
            self._day = day
            self._reset_session()
            self.trade = None
        m = et_minute_of_day(bar.ts)
        self.tr.append(bar.h - bar.l)

        orders: list[Order] = []
        if self.trade is not None:
            orders += self._manage(bar, m)
        elif self.pos == 0 and self._cool <= 0:
            orders += self._scan(bar, m)
        if self._cool > 0:
            self._cool -= 1
        self.c.append(bar.c)
        self.lo.append(bar.l)
        self.hi.append(bar.h)
        return orders

    def _scan(self, bar: Bar, m: int) -> list[Order]:
        if len(self.c) <= LOOKBACK or len(self.tr) < ATR_WIN:
            return []
        if m >= MOC_MIN - HOLD:
            return []
        atr = sum(self.tr) / len(self.tr)
        if atr <= 0:
            return []
        c = list(self.c)
        ret30 = (bar.c - c[-LOOKBACK]) / atr
        ret5 = (bar.c - c[-TURN]) / atr
        prev5 = (c[-1] - c[-TURN - 1]) / atr        # the same test one bar ago

        for d in (+1, -1):
            # the move ran AGAINST this direction, and has just turned back
            if ret30 * d >= 0 or ret5 * d <= 0:
                continue
            if prev5 * d > 0:
                continue          # already turned last bar -- take the FIRST bar only
            if self.use_window and not (WIN_OPEN <= m < WIN_CLOSE):
                continue
            # stop at the extreme of the move being faded: structural, not a distance
            ext = min(self.lo) if d > 0 else max(self.hi)
            risk_pts = abs(bar.c - ext)
            if risk_pts <= 0:
                continue
            size = position_size(RISK, risk_pts, self.point_usd, SIZE_CAP)
            if size <= 0:
                continue
            self.trade = _Pos(dir=d, entry=bar.c, stop=ext, size=size)
            return [Order(self.symbol, d, size, tag="fadeturn-entry")]
        return []

    def _manage(self, bar: Bar, m: int) -> list[Order]:
        t = self.trade
        assert t is not None
        d = t.dir
        t.age += 1

        def close(tag: str, at: float | None = None) -> list[Order]:
            self.trade = None
            self._cool = COOL
            return [Order(self.symbol, -d, t.size, tag=tag, reduce_only=True,
                          trigger_price=at)]

        if m >= MOC_MIN or t.age >= HOLD:
            return close("fadeturn-time")
        if self.stop_mode == "structural":
            stop_hit = (bar.l <= t.stop) if d > 0 else (bar.h >= t.stop)
            if stop_hit:
                return close("fadeturn-stop", at=level_fill(bar, t.stop, rising=d < 0))
        return []
