"""VwapBreakStrategy — trade the BREAK of the RTH session-VWAP band, WITH the
break (continuation), not against it.

Motivated by tools/avwap_study.py: on the ES sample, the session-open VWAP is
the one anchor with a *significant* reaction — and it is NEGATIVE. Price that
tests session VWAP tends to break THROUGH it (~+0.7pt/15m of continuation),
not revert. So the honest way to trade session VWAP is the break, not the fade.

The danger is chop: VWAP gets crossed many times on a balance day, and fading
or chasing every re-cross bleeds. Three guards:
  1. Require a close beyond the +/- k*sigma VWAP band (a genuine expansion, not
     a wiggle across the line) — sigma is the volume-weighted dispersion from
     AnchoredVWAP.
  2. AND require a new intraday extreme close, so mid-range pokes through the
     (small, early) band don't qualify — only real breakouts do.
  3. Bail the instant price falls back to the VWAP mean (continuation failed).
Gamma-aware (opt-in): breaks RUN on short-gamma days (dealers add to the move)
and FAIL on long-gamma days, so entries gate to short gamma. One position at a
time, two entries/day max, flat at 15:59 ET.

NOT a validated edge — a study-motivated sleeve that paper-trades alongside the
rest until it earns (or fails to earn) a live slot.
"""
from __future__ import annotations

from ..core.events import Bar
from ..core.exits import ExitCtx, TwoPhaseExit
from ..core.orders import Order
from ..core.timeutil import et_minute_of_day, et_session_date
from ..features.avwap import AnchoredVWAP
from .base import BaseStrategy

RTH_START = 9 * 60 + 30      # 09:30 — session VWAP starts here
START_MIN = 10 * 60          # 10:00 — no entries before this (sigma must settle)
EOD_FLAT = 15 * 60 + 59      # 15:59 — flat everything
MAX_ENTRIES = 2              # a failed break then a real one is common


class VwapBreakStrategy(BaseStrategy):
    def __init__(self, symbol: str, *, band_k: float = 1.0,
                 stop_mult: float = 1.0, trail_mult: float = 1.5,
                 stop_floor: float = 5.0, trail_floor: float = 8.0,
                 gamma=None, two_phase: TwoPhaseExit | None = None) -> None:
        self.symbol = symbol
        self.gamma = gamma
        self.band_k = band_k
        self.stop_mult, self.trail_mult = stop_mult, trail_mult
        self.stop_floor, self.trail_floor = stop_floor, trail_floor
        self.two_phase = two_phase
        self._day: str | None = None
        self.pos = 0
        self._reset_session()

    def _reset_session(self) -> None:
        self.av = AnchoredVWAP()                  # RTH session VWAP (from 09:30)
        self.sess_hi: float | None = None         # session high/low CLOSE so far
        self.sess_lo: float | None = None
        self.entries = 0
        self.trade: dict | None = None            # {side, entry, stop, trail, peak}

    def on_position(self, p) -> None:
        self.pos = p.qty

    def reset_for_live(self) -> None:
        self.trade = None
        self.pos = 0

    # ── main ─────────────────────────────────────────────────────────────────
    def on_bar(self, bar: Bar) -> list[Order]:
        if bar.tf != "1m":
            return []
        day = et_session_date(bar.ts)
        if day != self._day:
            self._day = day
            self._reset_session()
            if self.pos != 0:                     # never carry overnight
                side = 1 if self.pos > 0 else -1
                return [Order(self.symbol, -side, abs(self.pos), tag="safety-flat",
                              reduce_only=True)]
        m = et_minute_of_day(bar.ts)
        if m < RTH_START:                         # overnight: RTH VWAP not started
            return []
        if self.two_phase is not None:
            self.two_phase.note_price(bar.c, bar.ts)
        # accumulate the RTH session VWAP on this bar's typical price
        tp = (bar.h + bar.l + bar.c) / 3.0
        self.av.add(tp, float(bar.v) if bar.v else 0.0)
        if m >= EOD_FLAT:
            return self._flatten("moc")
        vwap, sigma = self.av.value, self.av.sigma
        up = vwap + self.band_k * sigma
        lo = vwap - self.band_k * sigma
        orders: list[Order] = []
        if self.pos != 0 and self.trade is not None:
            orders = self._manage(bar.c, vwap)
        elif (self.pos == 0 and self.entries < MAX_ENTRIES and m >= START_MIN
              and sigma > 0 and self.sess_hi is not None):
            orders = self._maybe_enter(bar.ts, bar.c, up, lo)
        # track session extreme CLOSES (updated AFTER the break check)
        self.sess_hi = bar.c if self.sess_hi is None else max(self.sess_hi, bar.c)
        self.sess_lo = bar.c if self.sess_lo is None else min(self.sess_lo, bar.c)
        return orders

    # ── entry: break of the band that is ALSO a new session extreme ──────────
    def _maybe_enter(self, ts: int, c: float, up: float, lo: float) -> list[Order]:
        # a genuine expansion: beyond the +/-k*sigma band AND a fresh intraday
        # extreme (not a mid-range wiggle across the line — the chop that bleeds).
        if c > up and c > self.sess_hi:
            side = 1
        elif c < lo and c < self.sess_lo:
            side = -1
        else:
            return []
        if not self.gamma_entry_ok(ts, "short"):          # continuation needs short gamma
            return []
        w = max(1e-9, up - lo)                             # band width = 2*k*sigma
        stop = max(self.stop_floor, self.stop_mult * w)
        trail = max(self.trail_floor, self.trail_mult * w)
        self.trade = {"side": side, "entry": c, "stop": stop, "trail": trail, "peak": 0.0}
        if self.two_phase is not None:
            self.two_phase.start(side, c)
        self.entries += 1
        return [Order(self.symbol, side, 1, tag="entry-vwapbreak")]

    # ── management: bail to VWAP (thesis dead) or hard/trailing stop ─────────
    def _manage(self, c: float, vwap: float) -> list[Order]:
        t = self.trade
        side = t["side"]
        # continuation failed the moment price returns to the mean
        if (side > 0 and c <= vwap) or (side < 0 and c >= vwap):
            return self._flatten("vwap-fail")
        fe = (c - t["entry"]) * side
        t["peak"] = max(t["peak"], fe)
        if self.two_phase is not None:
            # 2026-07-28: this sleeve ran +127.75pt and closed at -14.50 on the
            # MOC, 142.25pt given back. Ride, then hunt the reversal.
            hit = self.two_phase.check(ExitCtx(ts=0, price=c, dir=side,
                                               entry_px=t["entry"], entry_ts=0,
                                               peak_fe=t["peak"]))
            if hit:
                return self._flatten(hit)
            return self._flatten("stop") if fe <= -t["stop"] else []
        if fe <= max(-t["stop"], t["peak"] - t["trail"]):
            return self._flatten("trail")
        return []

    def _flatten(self, why: str) -> list[Order]:
        if self.pos == 0 or self.trade is None:
            self.trade = None
            return []
        side = self.trade["side"]
        qty = abs(self.pos)
        self.trade = None
        return [Order(self.symbol, -side, qty, tag=f"vwb-{why}", reduce_only=True)]


__all__ = ["VwapBreakStrategy"]
