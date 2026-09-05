"""OvernightFadeStrategy — fade the OVERNIGHT move at the RTH open.

The mirror image of OvernightBreakStrategy, and the opposite thesis: onbreak
trades CONTINUATION through the overnight extremes, this trades the OVERSHOOT
of the move made while the US was asleep.

    entry     the first RTH bar, against the sign of the overnight move
    target    75% of |overnight move|
    stop      0.75x the overnight range
    exit      15:59 if neither barrier is touched

WHY THE WINDOW IS 18:00->09:30, not onbreak's 00:00->09:30: the mechanism is
that the move is positioned in thin hours by non-US participants and corrected
by US traders arriving, so the quantity of interest is the whole Globex session
before the open. Measured, 18:00->09:30 (the MOVE) beats prior-close->open (the
GAP): $182 vs $124 per trade.

WHY A TARGET AT ALL, given that this project's own record says barrier-touch
probability is geometry and a fixed target caps winners (see the gap-fill work
-- a FILL target on this same signal LOSES, -$178/trade, t=-3.13). Because this
target is not a fixed price level: it is a FRACTION OF THE MOVE BEING FADED. It
is sized to the thesis and scale-free -- no point constants anywhere, which is
what made the NQ test at the bottom of this docstring a clean one. It does cost
expectancy -- $265 -> $181 per trade -- and buys a 4.4x smaller
drawdown and 52% -> 75% wins. Train 685 / test 458 ES sessions, config chosen on
the TRAIN half and scored once:

    variant                      TEST $/tr    t    win     maxDD   ret/DD
    no target, stop 1.5x               265  2.68   52%   -31,787     3.8
    target 75%, stop 0.75x  <- this    181  3.52   75%    -7,299    11.4
    target 50%, stop 1.5x              176  3.73   84%    -9,672     8.3
    target 33%, stop 1.5x              107  2.76   87%    -9,349     5.2

The whole 33/50/75/100% x three-stop family is positive at t=2.4-4.5, so this is
a surface and not a cell. Note the 33% target has the HIGHEST win rate and the
WORST ret/DD: shrinking the target shrinks the winner and leaves the loser
untouched, so win rate above ~80% here is bought with expectancy.

NO SELECTIVITY FILTER, deliberately, and this was tested rather than assumed.
`clean` = |on_move|/on_rng looked like the best filter in the book ($270/trade
on half the sessions) and is an artifact: clean correlates +0.69 with |on_move|
and the target IS a fraction of |on_move|, so filtering on it selects big dollar
targets, not good trades. Per point of overnight move the test quintiles pay
6.7 / 7.6 / 9.6 / 9.1 / -0.5 -- the CLEANEST nights are the worst (54% win),
which is what you would expect: a clean directional overnight is a trend, not an
overshoot. As policy, flat-1x-everything beat the gate (ret/DD 11.4 vs 10.0) and
both sizing schemes. Take every session, size flat.

ES ONLY. NQ was tested with these exact parameters over 194 sessions and fails:
$94/trade at t=0.43 with a -$35,620 drawdown, against ES on the same window at
$207 and t=3.09 with -$8,426. It is the SIGNAL, not the tuning -- the whole
barrier family is flat there (t from 1.05 down to -0.10, where every ES cell ran
2.4-4.5) while the ordering within it ports intact, so the barriers work and the
drift underneath them is absent. This is the second overnight-structure signal
to work on ES and die on NQ; see the failed-break fade.

NOT yet tested: slippage beyond the flat round turn.
"""
from __future__ import annotations

from ..core.events import Bar
from ..core.orders import Order
from ..core.timeutil import et_minute_of_day, et_session_date
from .base import BaseStrategy, level_fill

RTH_START = 9 * 60 + 30       # 09:30 ET — the overnight window closes, entry here
GLOBEX_OPEN = 18 * 60         # 18:00 ET — the overnight window opens
EOD_FLAT = 15 * 60 + 59       # 15:59 ET — flat
TGT_FRAC = 0.75               # target = this much of |overnight move|
STOP_FRAC = 0.75              # stop   = this much of the overnight RANGE
MIN_ON_BARS = 200             # a partial night is not an overnight move


def _next_day(day: str) -> str:
    """Calendar date string + 1 day."""
    from datetime import date, timedelta
    y, m, d = (int(x) for x in day.split("-"))
    return (date(y, m, d) + timedelta(days=1)).isoformat()


class OvernightFadeStrategy(BaseStrategy):
    def __init__(self, symbol: str, *, tgt_frac: float = TGT_FRAC,
                 stop_frac: float = STOP_FRAC, min_on_bars: int = MIN_ON_BARS,
                 gamma=None) -> None:
        self.symbol = symbol
        self.gamma = gamma
        self.tgt_frac = tgt_frac
        self.stop_frac = stop_frac
        self.min_on_bars = min_on_bars
        self._sday: str | None = None
        self.pos = 0
        self._reset_session()

    def _reset_session(self) -> None:
        self.on_first: float | None = None     # first close of the overnight window
        self.on_last: float | None = None      # last close before the open
        self.on_hi: float | None = None
        self.on_lo: float | None = None
        self.n_on = 0
        self.done = False                      # the open has been and gone
        self.trade: dict | None = None

    # ── plumbing ─────────────────────────────────────────────────────────────
    def on_position(self, p) -> None:
        self.pos = p.qty

    def reset_for_live(self) -> None:
        self.trade = None
        self.pos = 0

    @staticmethod
    def session_key(ts: int) -> str:
        """The RTH date this bar belongs to. et_session_date is the ET CALENDAR
        date and does not roll at the Globex open, so a 20:00 Monday bar carries
        Monday while the session it opens is Tuesday. Everything here is keyed
        to the RTH date so the overnight window and the open it precedes share
        one key."""
        day = et_session_date(ts)
        return _next_day(day) if et_minute_of_day(ts) >= GLOBEX_OPEN else day

    # ── main ─────────────────────────────────────────────────────────────────
    def on_bar(self, bar: Bar) -> list[Order]:
        if bar.tf != "1m":
            return []
        self.last_seen_px = bar.c
        sday = self.session_key(bar.ts)
        if sday != self._sday:
            self._sday = sday
            self._reset_session()
            if self.pos != 0:                  # never carry across a session
                side = 1 if self.pos > 0 else -1
                return [Order(self.symbol, -side, abs(self.pos), tag="safety-flat",
                              reduce_only=True)]
        m = et_minute_of_day(bar.ts)
        if m >= GLOBEX_OPEN or m < RTH_START:  # ── the overnight window
            if self.on_first is None:
                self.on_first = bar.c
            self.on_last = bar.c
            self.on_hi = bar.h if self.on_hi is None else max(self.on_hi, bar.h)
            self.on_lo = bar.l if self.on_lo is None else min(self.on_lo, bar.l)
            self.n_on += 1
            return []
        # ── the session ──
        if m >= EOD_FLAT:
            return self._flatten(bar, "moc", at=None)
        if self.pos != 0 and self.trade is not None:
            return self._manage(bar)
        if not self.done:                      # the FIRST bar past 09:30, or never
            return self._maybe_enter(bar)
        return []

    # ── entry: one shot, at the open, against the night ─────────────────────
    def _maybe_enter(self, bar: Bar) -> list[Order]:
        # One shot whatever happens. If the night was unusable this sleeve
        # stands down for the day rather than entering late — the thesis is
        # about the open, so a 10:47 entry is a different trade.
        self.done = True
        if self.n_on < self.min_on_bars or self.on_first is None:
            return []
        move = self.on_last - self.on_first
        rng = self.on_hi - self.on_lo
        if move == 0.0 or rng <= 0.0:
            return []
        side = -1 if move > 0 else 1           # fade it
        if not self.gamma_entry_ok(bar.ts, "long", bar.c):   # mean reversion
            return []
        entry = bar.c                          # the sim and the live router both
        self.trade = {                         # fill a market order at last price
            "side": side, "entry": entry,
            "target": entry + side * self.tgt_frac * abs(move),
            "stop": entry - side * self.stop_frac * rng,
        }
        return [Order(self.symbol, side, 1, tag="entry-onfade")]

    # ── management: stop first, then target (the conservative order) ────────
    def _manage(self, bar: Bar) -> list[Order]:
        t = self.trade
        side, stop, tgt = t["side"], t["stop"], t["target"]
        if (side > 0 and bar.l <= stop) or (side < 0 and bar.h >= stop):
            return self._flatten(bar, "stop", at=stop, rising=side < 0)
        if (side > 0 and bar.h >= tgt) or (side < 0 and bar.l <= tgt):
            return self._flatten(bar, "target", at=tgt, rising=side > 0)
        return []

    def _flatten(self, bar: Bar, why: str, at: float | None,
                 rising: bool = False) -> list[Order]:
        if self.pos == 0 or self.trade is None:
            self.trade = None
            return []
        side = self.trade["side"]
        qty = abs(self.pos)
        self.trade = None
        # A level-triggered exit prices at its LEVEL (or the open if the bar
        # gapped past it), never at the bar close — see Order.trigger_price.
        px = level_fill(bar, at, rising) if at is not None else None
        return [Order(self.symbol, -side, qty, tag=f"onfade-{why}",
                      reduce_only=True, trigger_price=px)]


__all__ = ["OvernightFadeStrategy"]
