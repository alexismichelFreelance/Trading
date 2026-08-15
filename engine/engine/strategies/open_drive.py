"""OpenDriveStrategy — first-30-minute drive continuation (one decision/day).

At 10:00 ET, enter in the direction of the 9:30->10:00 move; risk is scaled to
the morning's own range: hard stop = 1.0 x R30 (floor 5pt), trailing stop =
1.5 x R30 (floor 8pt) behind the peak, flat at 16:00 ET (MOC). No book data,
no flow, no regime model — price only, so it runs identically on any feed.

Evidence (see tools/open_drive_oracle CLI / tests/parity/test_parity_opendrive.py):
1-second path, 64 sec-covered days: +815 pts, mean +12.7/day, all 4 months
positive, both contracts positive, rho=-0.01 daily vs the ignition sleeve;
ESM5 Mar20-31 holdout +154 pts (7 days). Known limits: convexity profile
(April-heavy), t~1.6, fixed-point stops DIE on the 1s path (hence range-scaled).
"""
from __future__ import annotations

from ..core.events import Bar, BookFlow, Trade
from ..core.exits import ExitCtx, TwoPhaseExit
from ..core.orders import Order
from ..core.timeutil import et, et_session_date
from .base import BaseStrategy

ENTRY_MIN = 10 * 60          # 10:00 ET, minutes-of-day
FLAT_MIN = 15 * 60 + 59      # flatten on the first tick in the 15:59 ET minute
                             # (NOT 16:00 — the ESH5/EST data window ends 15:59:59 ET,
                             # so a 16:00 trigger would never fire and carry overnight)


class OpenDriveStrategy(BaseStrategy):
    def __init__(self, symbol: str, *, stop_mult: float = 1.0, trail_mult: float = 1.5,
                 stop_floor: float = 5.0, trail_floor: float = 8.0,
                 gamma=None, mode: str = "drive",
                 two_phase: TwoPhaseExit | None = None) -> None:
        self.symbol = symbol
        # optional GammaRegime: entries only on short-gamma days (strategy choice)
        self.gamma = gamma
        # mode="drive" (default, PARITY): blind 10:00 entry in the 9:30->10:00
        #   direction — the validated but naive rule (ravaged on a fakeout open).
        # mode="orb": wait past 10:00 for a real breakout of the 9:30-10:00 range,
        #   and with gamma, REFUSE the counter-gamma break (short gamma => don't
        #   buy the up-fakeout; take the resumed down-break). Directly fixes the
        #   2026-07-23 -41pt: it would have shorted the resume, not bought the poke.
        self.mode = mode
        # Optional RIDE-then-PROTECT exit. When present it REPLACES the trailing
        # stop as the decision: 2026-07-28 NQ:opendrive held a loser 87 min to
        # -346 on that trail while onbreak cut the same entry at -118, and on the
        # winners the trail gave back 177pt. The hard stop stays as insurance.
        self.two_phase = two_phase
        self.stop_mult, self.trail_mult = stop_mult, trail_mult
        self.stop_floor, self.trail_floor = stop_floor, trail_floor
        self._day: str | None = None
        self._reset_day()
        self.pos = 0

    def _reset_day(self) -> None:
        self.open_px: float | None = None
        self.hi = -float("inf")
        self.lo = float("inf")
        self.entered = False
        self.side = 0
        self.entry_px = 0.0
        self.stop = 0.0
        self.trail = 0.0
        self.peak_fe = 0.0
        self._or_hi = self._or_lo = None       # opening range, frozen at 10:00 (orb)

    # price ticks arrive as per-second trades (replay/live) — drive on pxc
    def on_trade(self, t: Trade) -> list[Order]:
        return self._step(t.ts, t.price)

    def on_bar(self, b: Bar) -> list[Order]:
        # bars-only feeds (no per-second stream) still work, on closes
        return self._step(b.ts, b.c) if b.tf == "1m" else []

    def on_position(self, p) -> None:
        self.pos = p.qty

    def reset_for_live(self) -> None:
        # clear phantom position from suppressed warmup entries. Do NOT re-arm
        # `entered`: the 10:00 entry is time-gated — if it already passed in the
        # backfill, today's shot is genuinely gone (re-arming would enter stale).
        self.pos = 0
        self.side = 0

    # ── core logic ───────────────────────────────────────────────────────
    def _step(self, ts: int, px: float) -> list[Order]:
        if self.two_phase is not None:
            self.two_phase.note_price(px, ts)   # per-TRADE here; minute-bucketed
        t = et(ts)
        mod = t.hour * 60 + t.minute
        day = et_session_date(ts)
        if day != self._day:
            self._day = day
            self._reset_day()
            if self.pos != 0:            # safety: never carry overnight
                side = 1 if self.pos > 0 else -1
                return [Order(self.symbol, -side, abs(self.pos), tag="safety-flat",
                              reduce_only=True)]
        if mod < 9 * 60 + 30 or mod > FLAT_MIN:
            return []
        # session-open tracking (9:30 onward)
        if mod < ENTRY_MIN:
            if self.open_px is None:
                self.open_px = px
            self.hi = max(self.hi, px)
            self.lo = min(self.lo, px)
            return []
        # end-of-day flat (15:59 ET)
        if mod >= FLAT_MIN:
            if self.pos != 0:
                side = 1 if self.pos > 0 else -1
                self.side = 0
                return [Order(self.symbol, -side, abs(self.pos), tag="moc", reduce_only=True)]
            return []
        # entry
        if not self.entered:
            if self.mode == "orb":
                return self._orb_entry(ts, px)
            # --- drive mode (default, PARITY): blind 10:00 entry ---
            self.entered = True
            if self.open_px is None:
                return []
            if not self.gamma_entry_ok(ts, "short"):
                return []                # non-short-gamma day: stand down (opt-in)
            r30 = px - self.open_px
            if r30 == 0:
                return []
            self.side = 1 if r30 > 0 else -1
            self.entry_px = px
            rng30 = max(self.hi, px) - min(self.lo, px)
            self.stop = max(self.stop_floor, self.stop_mult * rng30)
            self.trail = max(self.trail_floor, self.trail_mult * rng30)
            self.peak_fe = 0.0
            if self.two_phase is not None:
                self.two_phase.start(self.side, self.entry_px)
            return [Order(self.symbol, self.side, 1, tag="entry-opendrive")]
        # manage
        if self.pos != 0 and self.side != 0:
            fe = (px - self.entry_px) * self.side
            self.peak_fe = max(self.peak_fe, fe)
            if self.two_phase is not None:
                hit = self.two_phase.check(ExitCtx(ts=ts, price=px, dir=self.side,
                                                   entry_px=self.entry_px,
                                                   entry_ts=ts, peak_fe=self.peak_fe))
                if hit or fe <= -self.stop:      # thesis exit, else disaster stop
                    side = self.side
                    self.side = 0
                    return [Order(self.symbol, -side, abs(self.pos),
                                  tag=hit or "stop", reduce_only=True)]
                return []
            if fe <= max(-self.stop, self.peak_fe - self.trail):
                side = self.side
                self.side = 0
                return [Order(self.symbol, -side, abs(self.pos), tag="trail", reduce_only=True)]
        return []

    # ── ORB entry (mode="orb"): break of the 9:30-10:00 range, gamma-biased ──
    def _orb_entry(self, ts: int, px: float) -> list[Order]:
        if self._or_hi is None:                 # freeze the opening range at 10:00
            if self.open_px is None:
                self.entered = True             # no open data -> no trade today
                return []
            self._or_hi, self._or_lo = self.hi, self.lo
        allow_long = allow_short = True
        if self.gamma is not None:
            sg = self.gamma.is_short_gamma(et_session_date(ts))
            if sg is True:                      # short gamma: fade up-pokes, take downs
                allow_long = False
            elif sg is False:                   # long gamma: take ups only
                allow_short = False
        d = 1 if (px >= self._or_hi and allow_long) else \
            (-1 if (px <= self._or_lo and allow_short) else 0)
        if d == 0:
            return []                           # keep waiting for an allowed break
        self.entered = True
        if not self.pocket_entry_ok(px):        # see BaseStrategy.pocket_entry_ok
            return []
        self.side = d
        self.entry_px = px
        rng = max(1e-9, self._or_hi - self._or_lo)
        self.stop = max(self.stop_floor, self.stop_mult * rng)
        self.trail = max(self.trail_floor, self.trail_mult * rng)
        self.peak_fe = 0.0
        if self.two_phase is not None:
            self.two_phase.start(d, self.entry_px)
        return [Order(self.symbol, d, 1, tag="entry-orb")]


__all__ = ["OpenDriveStrategy"]
