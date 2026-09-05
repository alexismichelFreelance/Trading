"""TrendJoinStrategy — join a directional leg after it proves itself, and HOLD.

Built from a measurement of the moves themselves rather than from a hypothesis
about what should predict them (strategy_lab/move_catalog.py and
move_catchability.py, ESH5+ESM5, 63 sessions of 1m bars):

  * an RTH session contains ~3.8 directional legs of >=20pt, median 39.8pt and
    median 47 MINUTES. 1.9 per session are 40pt+, median 60pt. 98% of sessions
    have at least one. That is the target; nothing else intraday is worth the
    risk.
  * they are catchable LATE. At 15pt of confirmation a >=20pt leg still has a
    median 23.8pt to give against 7.6pt of heat (3.1:1), and a 40pt+ leg has
    43.4pt left with an hour still to run.
  * confirmation IS the filter. Legs >=20pt reach 15pt of confirmation 98% of
    the time; sub-20pt legs only 47%, and have 2.0pt left when they do. There is
    no separate predictor to find -- waiting is the predictor.

Every parameter is a measured quantity, not a swept one:
    conf_pts 15   the level that separates big legs from small (98% vs 47%)
    stop_pts  8   the measured median MAE of a caught leg is 7.5pt
    hold_min 90   median leg duration is 47min; 42% run past an hour
    lookback 30   the window the confirmation is measured against

P&L EVIDENCE -- READ THIS BEFORE TRUSTING ANY NUMBER HERE.
The first figures quoted for this sleeve ($999 and $837 per session) are WRONG
and withdrawn. They came from a hand-written simulation whose stop model was
optimistic twice over: it triggered only when a bar CLOSED through the level (so
fewer stops than a real stop order), then booked exactly -stop_pts as if filled
at the level (a better price than the close it triggered on). Modelling each
mechanic faithfully gives $118-$614 per session depending on config -- a quarter
to a half of what was claimed. Live on 2026-08-03 the first stop was taken at
-12.75pt against an 8pt level, confirming the overshoot.

The deeper problem is that the simulation was a REIMPLEMENTATION of this class
rather than this class. Any number that does not come from running
TrendJoinStrategy itself through tools/portfolio_replay.py should be treated as
describing a different program, because it does. That validation is still
outstanding; until it exists this sleeve is paper-only on its own forward record.

What the catalogue does support (it is a measurement of the tape, not of a
strategy) is the parameter choice above.

WHY THE REST OF THE BOOK MISSES THESE: chop_stop is 4.0pt against a 7.5pt median
MAE, so it is ejected before the move works; and the intraday exits are 15-60
SECONDS against legs that run 47 minutes.

Intraday by design: holds_overnight stays False, so the engine's session flat
owns the close.
"""
from __future__ import annotations

from collections import deque

from ..core.events import Bar, Fill
from ..core.exits import ExitCtx, TwoPhaseExit
from ..core.orders import Order
from ..core.timeutil import et_minute_of_day
from .base import BaseStrategy

RTH_OPEN = 9 * 60 + 30
RTH_LAST_ENTRY = 15 * 60 + 30      # nothing new in the last half hour: the
                                   # catalog puts 1% of big legs after 15:00


class TrendJoinStrategy(BaseStrategy):
    holds_overnight = False

    def __init__(self, symbol: str, *, lookback: int = 30,
                 conf_pts: float = 15.0, stop_pts: float = 8.0,
                 hold_min: int = 90, qty: int = 1,
                 two_phase: TwoPhaseExit | None = None) -> None:
        self.symbol = symbol
        self.lookback = lookback
        self.conf_pts = conf_pts
        self.stop_pts = stop_pts
        self.hold_min = hold_min
        self.qty = qty
        # The clock was never an exit thesis. On 2026-08-03 the 90-minute cap cut
        # a +45.25pt winner at 11:05 while the day ran another 33pt to 7637 --
        # exactly the give-back TwoPhaseExit exists to stop. With a two_phase
        # attached the clock is demoted to what it always was: insurance, fired
        # only if nothing else has.
        self.two_phase = two_phase
        self._hi: deque[float] = deque(maxlen=lookback)
        self._lo: deque[float] = deque(maxlen=lookback)
        self.pos = 0
        self._trade: dict | None = None
        self._bars_held = 0
        # JOIN ON A PULLBACK instead of on the breakout bar. 0 = off.
        #
        # The sleeve enters the moment confirmation arrives -- price has already
        # travelled conf_pts off the window extreme, so it buys the move at its
        # most extended point so far, and its own record says that is expensive:
        # a caught leg gives a median 7.5pt of heat, and on 2026-08-28
        # ES:trendjoin_narrow took six stops in one session flipping direction
        # on every one.
        #
        # With pullback_pts set, confirmation only ARMS. The entry waits for
        # price to retrace that far from the extreme reached since arming, and
        # a move that never retraces is DECLINED -- that runaway is precisely
        # the one a breakout entry buys at its worst price. The arm expires
        # after pullback_bars so a stale setup cannot fire on old news.
        #
        # NOT a clock gate. "Skip the first hour" was tested over 29 ES / 26 NQ
        # replayed sessions and does not replicate -- it helps ES (+105,062 ->
        # +119,275 skipping before 10:30) and destroys NQ (+513,295 -> +110,160),
        # and the worst window differs per instrument (ES 10:00-10:30, NQ
        # 11:30-14:00). Time of day is noise here; the entry PRICE is not.
        self.pullback_pts = 0.0
        # PULL BACK TO STRUCTURE, not to a distance. With a LevelBook attached
        # the sleeve waits for price to retrace into whatever is actually there
        # -- a pivot, prior-day high, virgin zone edge, VWAP, a gamma wall --
        # and DECLINES when nothing is. `pullback_pts` above is the magic-number
        # version and is kept only as the control to measure this against: 4pt
        # on ES became 20pt on NQ purely to preserve a ratio, which is the tell
        # that it describes no market fact at all.
        self.levels = None
        self.pullback_max = 0.0      # absolute cap, if one is set explicitly
        # HOW FAR BACK IS STILL A PULLBACK, as a fraction of a TYPICAL session's
        # range rather than a point count. A points cap is the same mistake as a
        # points pullback: 30 on ES would have to become 150 on NQ for no reason
        # the market recognises. DayRange.typical() is measured from the last ten
        # sessions, so this rescales itself when the regime does. Fails closed --
        # a cold ruler means no cap can be computed and no trade is taken.
        self.pullback_frac = 0.0
        # PULLBACK DEPTH as a fraction of a TYPICAL session's range. This is the
        # scale-free form of the only thing that replicated: 8 ES points and 40
        # NQ points both roughly doubled the baseline (+13,862 vs +7,212;
        # +42,605 vs +17,550), and those two independently-chosen constants land
        # at 0.138 and 0.107 of their instrument's median RTH range -- about an
        # eighth of a normal day, on both. Expressed this way it needs no
        # per-instrument value and rescales when the regime does.
        #
        # Chosen over the structure version, which FAILED -- but the reason
        # first recorded here was WRONG and is corrected 2026-09-04.
        #
        # It said confluence happens on 1 of 108 ES entries, so "strongest
        # cluster" degenerated to "nearest lone pivot". That rate was an
        # artifact: LevelBook.levels() called z.virgin() on a bool @property,
        # raising TypeError into a bare except, so NO ZONE EVER REACHED THE
        # BOOK. Confluence cannot involve an absent source. (Three more bugs sat
        # behind it: zones were detected on raw 1m bars rather than 30m, touches
        # were never counted so nothing left the virgin state, and the per-bar
        # loops walked every zone ever created.)
        #
        # All four fixed, confluence then available on 49% of moments instead of
        # 8% -- and the sleeve STILL loses. 56 ES sessions, replayed:
        #     trendjoin_narrow  $242/day   trendjoin_pb8  $225/day
        #     trendjoin_pb4     $198/day   trendjoin_lvl   $77/day
        # So the conclusion stands and the mechanism is now understood: waiting
        # for structure is worse than not waiting, even when the structure is
        # really there. It is not a plumbing failure.
        self.pullback_typ = 0.0
        self._arm_level: float | None = None
        self._arm_src = ""
        self.pullback_bars = 20
        self._arm_dir = 0
        self._arm_ext: float | None = None
        self._arm_age = 0
        # Lots to take on a day FOLLOWING a wider-than-usual session. 0 = off.
        # Needs a DayRange; see DayRange.size_mult for the measurement and for
        # why sizing is the only lever a fixed-ledger measurement can justify.
        self.size_wide = 0.0

    def on_bar(self, b: Bar) -> list[Order]:
        if b.tf != "1m":
            return []
        m = et_minute_of_day(b.ts)
        if self.two_phase is not None:
            self.two_phase.note_price(b.c, b.ts)

        # ── manage an open position BEFORE the window is updated, so the exit
        # decision uses the same bar the price is read from ──────────────────
        # Ordered by KIND per core/exits.py: THESIS first, INSURANCE last. The
        # stop and the clock are what is left over when nothing else fired.
        if self.day_range is not None:
            self.day_range.note(b.ts, b.c)
        if self.levels is not None:
            self.levels.on_bar(b)

        if self.pos != 0 and self._trade is not None:
            self._bars_held += 1
            d = self._trade["dir"]
            # SCALE OUT before any exit rule fires. The day's opportunity being
            # spent is not a reason to be flat -- it is a reason to be smaller.
            # See BaseStrategy.scale_out_qty for the evidence and the limits.
            n = self.scale_out_qty(b.c, self._trade["entry"], d, self.pos)
            if n:
                self._scaled = True
                return [Order(self.symbol, -d, n, tag="trendjoin-scale",
                              reduce_only=True)]
            if self.two_phase is not None:
                hit = self.two_phase.check(ExitCtx(
                    ts=b.ts, price=b.c, dir=d, entry_px=self._trade["entry"],
                    entry_ts=self._trade["ts"], minute_et=m))
                if hit:
                    return self._flatten(hit)
            if (b.c - self._trade["entry"]) * d <= -self.stop_pts:
                return self._flatten("stop")
            if self._bars_held >= self.hold_min:
                return self._flatten("timeout")

        if self._arm_dir:
            self._arm_age += 1
            if self._arm_age > self.pullback_bars:    # stale: forget it
                self._arm_dir, self._arm_ext, self._arm_age = 0, None, 0
                self._arm_level, self._arm_src = None, ""

        in_rth = m >= RTH_OPEN
        ready = len(self._hi) >= self.lookback
        win_hi = max(self._hi) if self._hi else None
        win_lo = min(self._lo) if self._lo else None
        self._hi.append(b.h)
        self._lo.append(b.l)

        if not (in_rth and ready) or self.pos != 0 or self._trade is not None:
            return []
        if m > RTH_LAST_ENTRY:
            return []
        # ALREADY ARMED: the retracement is checked on EVERY bar, independently
        # of whether confirmation still holds. It usually does not -- price has
        # pulled back, which is the entire point -- so testing the arm after the
        # confirmation branch would return early and the entry would never fire.
        if self._arm_dir:
            d = self._arm_dir
            self._arm_ext = (max(self._arm_ext, b.c) if d > 0
                             else min(self._arm_ext, b.c))
            if self._arm_level is not None:
                # STRUCTURE: has price actually traded into the level yet? The
                # bar's extreme, not its close -- a touch is a touch.
                reached = (b.l <= self._arm_level) if d > 0 else (b.h >= self._arm_level)
                if not reached:
                    return []
            else:
                need = self.pullback_pts
                if self.pullback_typ > 0.0:
                    typ = (self.day_range.typical()
                           if self.day_range is not None else None)
                    if typ is None:
                        return []                     # cold ruler: no trade
                    need = self.pullback_typ * typ
                if (self._arm_ext - b.c) * d < need:
                    return []                         # not deep enough yet
            if not self.pocket_entry_ok(b.c):
                return []
            src = self._arm_src
            self._arm_dir, self._arm_ext, self._arm_age = 0, None, 0
            self._arm_level, self._arm_src = None, ""
            out = self._open(d, b)
            if out and src:
                out[0] = Order(self.symbol, d, out[0].qty,
                               tag=f"trendjoin-entry-{src}")
            return out
        # confirmation measured against the window as it stood BEFORE this bar
        if b.c - win_lo >= self.conf_pts:
            d = 1
        elif win_hi - b.c >= self.conf_pts:
            d = -1
        else:
            return []
        # Near a gamma pocket edge a continuation entry is taken into
        # reversion (VR30 0.86 there against 1.25 deep). Checked BEFORE any
        # trade state is written, so a declined entry leaves the sleeve
        # genuinely flat rather than tracking a position it never opened.
        if not self.pocket_entry_ok(b.c):
            return []
        # PULLBACK: confirmation only ARMS; the entry waits for a retracement.
        cap = self.pullback_max
        if self.pullback_frac > 0.0:
            typ = self.day_range.typical() if self.day_range is not None else None
            if typ is None:
                return []                      # cold ruler: no opinion, no trade
            cap = self.pullback_frac * typ
        if self.levels is not None and cap > 0.0:
            # STRONGEST structure within reach, not the nearest. Sources within
            # `tol` are one level; tol scales with the day so it is not a point
            # count. Nearest-first is what made the first version a shallow
            # constant in disguise.
            typ = self.day_range.typical() if self.day_range is not None else None
            tol = 0.03 * typ if typ else 0.0
            hit = self.levels.pullback_to(b.c, d, max_dist=cap, tol=tol)
            if hit is None:
                return []          # nothing to pull back TO -- decline the setup
            self._arm_level, self._arm_src, _n = hit
            self._arm_dir, self._arm_ext, self._arm_age = d, b.c, 0
            return []
        if self.pullback_pts > 0.0 or self.pullback_typ > 0.0:
            self._arm_level, self._arm_src = None, ""
            self._arm_dir, self._arm_ext, self._arm_age = d, b.c, 0
            return []
        return self._open(d, b)

    def _open(self, d: int, b: Bar) -> list[Order]:
        self._trade = {"dir": d, "entry": b.c, "ts": b.ts}
        self._bars_held = 0
        self.scale_reset()
        if self.two_phase is not None:
            self.two_phase.start(d, b.c)
        return [Order(self.symbol, d, self._entry_qty(), tag="trendjoin-entry")]

    def _entry_qty(self) -> int:
        """Lots for THIS entry, from the prior session's range.

        The decision is taken once, before the open, and applies to the whole
        day -- it does not look at this trade, this minute or this price, so it
        cannot alter which trades the sleeve takes. That is the entire reason it
        is trustworthy where a filter or an exit rule is not: the 10-minute
        scratch predicted +3,575 on ES from a fixed ledger and delivered -6,262
        because it changed the sleeve from 213 trades to 293.

        Off unless size_wide is set. See DayRange.size_mult."""
        if self.size_wide <= 0.0 or self.day_range is None:
            return self.qty
        return int(self.qty * self.day_range.size_mult(wide=self.size_wide,
                                                       narrow=1.0))

    def _flatten(self, why: str) -> list[Order]:
        if self.pos == 0 or self._trade is None:
            self._trade = None
            return []
        d = self._trade["dir"]
        qty = abs(self.pos)
        self._trade = None
        self._bars_held = 0
        return [Order(self.symbol, -d, qty, tag=f"trendjoin-{why}",
                      reduce_only=True)]

    def on_position(self, p) -> None:
        self.pos = p.qty
        if p.qty == 0:
            self._trade = None
            self._bars_held = 0

    def reset_for_live(self) -> None:
        self.pos = 0
        self._trade = None
        self._bars_held = 0

    def on_fill(self, f: Fill) -> None:
        return None


__all__ = ["TrendJoinStrategy"]
