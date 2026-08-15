"""WallFadeStrategy — fade the gamma walls, but only in a LONG-gamma pocket.

THE EVIDENCE (strategy_lab/gamma_walls_and_onbreak.py, 30 ES sessions, 10,799
bars, forward 30-bar move, session-demeaned to remove the window's drift):

    regime  where                        bars    mean   up%
    LONG    within 10pt of CALL wall      531   -1.58   44%    caps
    LONG    within 10pt of PUT  wall      512   +1.66   59%    supports
    LONG    far from every wall           991   -1.10   47%

    SHORT   within 10pt of CALL wall    3,327   -0.81   48%
    SHORT   within 10pt of PUT  wall    2,763   -0.99   46%
    SHORT   far from every wall         1,308   +6.24   67%

In a long-gamma pocket dealers hedge AGAINST the move, so a call wall caps and a
put wall supports: -1.58 and +1.66, symmetric, both the right sign, both against
a -1.10 baseline. In a short-gamma pocket neither wall shows a clean effect --
both mildly negative, no "carried through" signature -- so this sleeve does not
trade there at all.

Tested unconditionally the effect VANISHES (call -0.53, put -0.87): 85% of bars
are short-gamma, so pooling drowns the long-gamma signal in the regime where it
does not exist. That is why this is gated rather than a plain level sleeve.

HONESTY ABOUT THE SAMPLE. 531 and 512 bars are overlapping 30-bar windows, so
the effective count is nearer 17 independent observations a side. Long-gamma
pockets were ~15% of bars in this window, so the sleeve stands down most days.
This is a hypothesis with a mechanism and the right sign, not a validated edge:
it deploys to PAPER and earns its way out.

Walls are RANKED, not singular -- the top 3 a side, because price meets whichever
is nearest, and the 2nd and 3rd are what the old single-wall row discarded.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.events import Bar
from ..core.orders import Order
from ..core.timeutil import et_minute_of_day, et_session_date
from .base import BaseStrategy, level_fill

RTH_OPEN, RTH_CLOSE = 9 * 60 + 30, 16 * 60
NEAR = 10.0          # points that count as "at" a wall (the tested distance)
STOP = 12.0          # beyond the wall: the level is wrong, not early
TARGET = 20.0        # ~the 30-bar move the study measured, taken as a target
MAX_BARS = 30        # the horizon the edge was measured over; past it, no claim
MAX_PER_DAY = 2


@dataclass
class _Trade:
    dir: int
    entry: float
    stop: float
    target: float
    bars: int = 0


class WallFadeStrategy(BaseStrategy):
    def __init__(self, symbol: str, point_usd: float = 50.0,
                 near: float = NEAR, qty: int = 1) -> None:
        self.symbol = symbol
        self.point_usd = point_usd
        self.near = near
        self.qty = qty
        self.pos = 0
        self.trade: _Trade | None = None
        self._day: str | None = None
        self._n = 0
        # set per session by the runner/replay; None = no curve, no trading
        self.pocket = None

    def on_position(self, p) -> None:
        self.pos = p.qty

    def reset_for_live(self) -> None:
        self.trade = None
        self.pos = 0

    def on_bar(self, bar: Bar) -> list[Order]:
        day = et_session_date(bar.ts)
        if day != self._day:
            self._day, self._n, self.trade = day, 0, None
        if self.session_over(bar.ts) and self.pos != 0:
            d = 1 if self.pos > 0 else -1
            qty, self.trade = abs(self.pos), None
            return [Order(self.symbol, -d, qty, tag="wallfade-flat",
                          reduce_only=True)]
        m = et_minute_of_day(bar.ts)
        if not (RTH_OPEN <= m < RTH_CLOSE):
            return []
        if self.trade is not None:
            return self._manage(bar)
        if self.pos or self._n >= MAX_PER_DAY or self.pocket is None:
            return []
        return self._scan(bar)

    # ── entry ────────────────────────────────────────────────────────────
    def _scan(self, b: Bar) -> list[Order]:
        a = self.pocket.at(b.c)
        if a is None or a.get("local_sign", 0) <= 0:
            return []                      # SHORT pocket: no clean wall effect
        # NEAREST wall, direction from its NET. A strike can be the biggest
        # call concentration AND the biggest put one -- SPX on 2026-08-14 had
        # 7800 and 8000 in both ranked lists -- so the lists cannot say whether
        # a level caps or supports. The net at the strike can: call-heavy (net
        # > 0) caps, put-heavy (net < 0) supports. Ranking by |net| also means
        # the sleeve trades the concentrations that actually dominate rather
        # than whichever list happened to be checked first.
        walls = [(w, n) for w, n in (a.get("walls") or [])
                 if b.l - self.near <= w <= b.h + self.near and n != 0]
        if not walls:
            return []
        lvl, net = min(walls, key=lambda x: abs(x[0] - b.c))
        if net > 0:                        # call-heavy: a cap, fade it short
            return self._enter(-1, level_fill(b, lvl, rising=True), "call")
        return self._enter(1, level_fill(b, lvl, rising=False), "put")

    def _enter(self, d: int, px: float, which: str) -> list[Order]:
        self.trade = _Trade(d, px, px - d * STOP, px + d * TARGET)
        self._n += 1
        # priced AT the wall (gap-adjusted): the level is the trade, so the fill
        # has to be the level or the stop and target mean nothing.
        return [Order(self.symbol, d, self.qty, tag=f"wallfade-{which}",
                      trigger_price=px)]

    # ── management ───────────────────────────────────────────────────────
    def _manage(self, b: Bar) -> list[Order]:
        t = self.trade
        assert t is not None
        t.bars += 1
        d = t.dir

        def close(tag: str, at: float | None) -> list[Order]:
            self.trade = None
            if self.pos == 0:
                return []
            return [Order(self.symbol, -d, abs(self.pos), tag=f"wallfade-{tag}",
                          reduce_only=True, trigger_price=at)]

        stop_hit = (b.l <= t.stop) if d > 0 else (b.h >= t.stop)
        tgt_hit = (b.h >= t.target) if d > 0 else (b.l <= t.target)
        if stop_hit:                       # stop first: the bar cannot say which
            return close("stop", level_fill(b, t.stop, rising=d < 0))
        if tgt_hit:
            return close("target", level_fill(b, t.target, rising=d > 0))
        if t.bars >= MAX_BARS:             # past the measured horizon
            return close("timeout", None)
        return []


__all__ = ["WallFadeStrategy"]
