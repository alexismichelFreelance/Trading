"""FadeLadderStrategy — the mechanism his P&L actually comes from.

DECOMPOSITION OF HIS $946,422 OF INTRADAY P&L (296 episodes, from his own
fills, no modelling):

    his first entry only, 1 lot, held to his exit          $ 56,932
    his first entry only, at his full size                 $364,046
    his AVERAGE entry price, at his full size              $930,721
    what he actually made                                  $946,422

Scaling IN is worth $566,674 -- 60% of it. Scaling OUT is worth $15,702, i.e.
nothing. **The entry is not the edge; the ladder is.** That is why every attempt
to reproduce him from the entry moment failed: fade_turn.py matched his entries
8% of the time and lost money, and a gradient booster given 25 causal features
and 98,550 minutes had NEGATIVE out-of-sample skill.

THE LADDER, measured from 1,804 of his add-fills:
    91% of adds are at a better price than the first fill, 93% better than the
    running average. Uniform 1-lot clips (87% ES / 95% NQ identical to the first
    fill) -- NOT a martingale. Cumulative depth by add number on ES runs
    0.25 0.50 0.75 1.00 1.25 1.50 1.75 2.00: one tick further against per add.
    Median add sits 1.00 pt below the running average on ES, 3.71 on NQ --
    0.40 and 0.37 ATR respectively. The SAME number on both instruments, which
    is why STEP_ATR is expressed in ATR and not in points.

RISK. Averaging down with no stop has unbounded tail risk, and his record is
SIM, where nothing margin-calls. His intraday damage was small (worst episode
-$2,954) but his 1-3 day bucket LOST $140,717 at a 75% win rate -- that is this
method failing when the move does not come back. MAX_LOTS is therefore a hard
cap, and the day-stop is the real risk control. A backtest cannot price ruin.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from ..core.events import Bar
from ..core.orders import Order
from ..core.timeutil import et_minute_of_day, et_session_date
from .base import BaseStrategy

WIN_OPEN, WIN_CLOSE = 8 * 60, 11 * 60
LOOKBACK, TURN, ATR_WIN = 30, 5, 30
STEP_ATR = 0.40            # his measured add spacing, same on ES and NQ
HOLD = 84                  # his median intraday hold, minutes
MOC_MIN = 959
COOL = 15
MAX_LOTS = 10
DAY_STOP_ATR = 12.0        # abandon the ladder if it runs this far past the avg


@dataclass
class _Pos:
    dir: int
    avg: float
    lots: int
    last_add: float
    age: int = 0
    adds: int = 0


class FadeLadderStrategy(BaseStrategy):
    def __init__(self, symbol: str, point_usd: float = 50.0,
                 use_window: bool = True, max_lots: int = MAX_LOTS,
                 ladder: bool = True, target_atr: float = 0.0,
                 time_stop: int | None = HOLD) -> None:
        self.symbol = symbol
        self.point_usd = point_usd
        self.use_window = use_window
        self.max_lots = max_lots
        self.ladder = ladder          # False = one clip, the control
        self.target_atr = target_atr  # 0 = hold to the time stop, like him
        # time_stop=None holds to the session close instead of cutting at HOLD.
        # Measured: once a trade is 1 ATR against, it returns to the entry
        # within 2h 94% of the time -- IDENTICAL for his entries and this
        # sleeve's (93.8% vs 94.5%). So a fixed 84-minute exit cuts trades that
        # were about to recover, which is what killed the first ladder run.
        self.time_stop = time_stop
        self.pos = 0
        self.trade: _Pos | None = None
        self._day: str | None = None
        self._reset()

    def _reset(self) -> None:
        self.c: deque[float] = deque(maxlen=LOOKBACK + 2)
        self.tr: deque[float] = deque(maxlen=ATR_WIN)
        self._cool = 0

    def on_position(self, p) -> None:
        self.pos = p.qty

    def reset_for_live(self) -> None:
        self.trade = None
        self.pos = 0

    def on_bar(self, bar: Bar) -> list[Order]:
        day = et_session_date(bar.ts)
        if day != self._day:
            self._day = day
            self._reset()
            self.trade = None
        m = et_minute_of_day(bar.ts)
        self.tr.append(bar.h - bar.l)
        out: list[Order] = []
        if self.trade is not None:
            out += self._manage(bar, m)
        elif self.pos == 0 and self._cool <= 0:
            out += self._scan(bar, m)
        if self._cool > 0:
            self._cool -= 1
        self.c.append(bar.c)
        return out

    def _atr(self) -> float:
        return sum(self.tr) / len(self.tr) if self.tr else 0.0

    def _scan(self, bar: Bar, m: int) -> list[Order]:
        if len(self.c) <= LOOKBACK or len(self.tr) < ATR_WIN:
            return []
        if m >= MOC_MIN - (self.time_stop or 120):
            return []
        atr = self._atr()
        if atr <= 0:
            return []
        c = list(self.c)
        ret30 = (bar.c - c[-LOOKBACK]) / atr
        ret5 = (bar.c - c[-TURN]) / atr
        prev5 = (c[-1] - c[-TURN - 1]) / atr
        for d in (+1, -1):
            if ret30 * d >= 0 or ret5 * d <= 0 or prev5 * d > 0:
                continue
            if self.use_window and not (WIN_OPEN <= m < WIN_CLOSE):
                continue
            self.trade = _Pos(dir=d, avg=bar.c, lots=1, last_add=bar.c)
            return [Order(self.symbol, d, 1, tag="ladder-entry")]
        return []

    def _manage(self, bar: Bar, m: int) -> list[Order]:
        t = self.trade
        assert t is not None
        d = t.dir
        t.age += 1
        atr = self._atr() or 1.0
        adverse = (t.avg - bar.c) * d          # >0 = the trade is against us

        def close(tag: str) -> list[Order]:
            self.trade = None
            self._cool = COOL
            return [Order(self.symbol, -d, t.lots, tag=tag, reduce_only=True)]

        if m >= MOC_MIN or (self.time_stop is not None and t.age >= self.time_stop):
            return close("ladder-time")
        if adverse >= DAY_STOP_ATR * atr:
            return close("ladder-abandon")
        if self.target_atr > 0 and (bar.c - t.avg) * d >= self.target_atr * atr:
            return close("ladder-target")
        # add one lot for every STEP_ATR further against the running average
        if self.ladder and t.lots < self.max_lots:
            if (t.last_add - bar.c) * d >= STEP_ATR * atr:
                px = bar.c
                t.avg = (t.avg * t.lots + px) / (t.lots + 1)
                t.lots += 1
                t.adds += 1
                t.last_add = px
                return [Order(self.symbol, d, 1, tag="ladder-add")]
        return []
