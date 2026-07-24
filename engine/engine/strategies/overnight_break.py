"""OvernightBreakStrategy — trade the RTH break of the OVERNIGHT range.

Sibling of VwapBreakStrategy: same continuation thesis, different anchor. The
overnight (00:00-09:30 ET, the Asia-late/London window) high and low are the
levels the user actually reads for the daily bias, and tools/avwap_study.py
--source live found the overnight-anchored AVWAP carries the same CONTINUATION
signature as session VWAP (ES overnight_lo reacted -1.02pt, t=-2.22: tests
resolve by breaking through, not reverting).

So: freeze the overnight range at 09:30, then take the first RTH close beyond
it, in the direction of the break. Guards against the classic bear/bull trap:
  1. No entry before START_MIN — the 09:30-10:00 poke is the fakeout window
     (exactly what cost opendrive_orb -24.75pt on 2026-07-24).
  2. Require the break to clear the level by a buffer scaled to the overnight
     range, not a fixed point count (so it ports to NQ unchanged).
  3. Bail immediately if price closes back INSIDE the range — a failed break.
Gamma-aware (opt-in): breaks run on short-gamma days. One position, one entry
per side per day, flat at 15:59 ET.

NOT a validated edge — paper-only until it earns a live slot. In particular the
overnight anchors are the THINNEST part of the study (ES 28 sessions, NQ 8), so
this sleeve exists partly to accumulate its own forward record.
"""
from __future__ import annotations

from ..core.events import Bar
from ..core.orders import Order
from ..core.timeutil import et_minute_of_day, et_session_date
from .base import BaseStrategy

RTH_START = 9 * 60 + 30      # 09:30 — overnight range freezes here
START_MIN = 10 * 60          # 10:00 — no entries before this (fakeout window)
EOD_FLAT = 15 * 60 + 59      # 15:59 — flat everything
BUF_FRAC = 0.05              # break buffer = 5% of the overnight range
MIN_RANGE = 5.0              # points; below this the night was too quiet to trade


class OvernightBreakStrategy(BaseStrategy):
    def __init__(self, symbol: str, *, buf_frac: float = BUF_FRAC,
                 stop_mult: float = 0.5, trail_mult: float = 0.75,
                 stop_floor: float = 5.0, trail_floor: float = 8.0,
                 min_range: float = MIN_RANGE, gamma=None) -> None:
        self.symbol = symbol
        self.gamma = gamma
        self.buf_frac = buf_frac
        self.stop_mult, self.trail_mult = stop_mult, trail_mult
        self.stop_floor, self.trail_floor = stop_floor, trail_floor
        self.min_range = min_range
        self._day: str | None = None
        self.pos = 0
        self._reset_session()

    def _reset_session(self) -> None:
        self.on_hi: float | None = None       # overnight range, accumulated pre-09:30
        self.on_lo: float | None = None
        self.frozen = False                   # True once RTH starts
        self.used: set[int] = set()           # sides already traded today
        self.trade: dict | None = None

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
            if self.pos != 0:                 # never carry overnight
                side = 1 if self.pos > 0 else -1
                return [Order(self.symbol, -side, abs(self.pos), tag="safety-flat",
                              reduce_only=True)]
        m = et_minute_of_day(bar.ts)
        if m < RTH_START:                     # build the overnight range
            self.on_hi = bar.h if self.on_hi is None else max(self.on_hi, bar.h)
            self.on_lo = bar.l if self.on_lo is None else min(self.on_lo, bar.l)
            return []
        self.frozen = True                    # 09:30: the range is now fixed
        if m >= EOD_FLAT:
            return self._flatten("moc")
        if self.pos != 0 and self.trade is not None:
            return self._manage(bar.c)
        if self.pos == 0 and m >= START_MIN:
            return self._maybe_enter(bar.ts, bar.c)
        return []

    # ── entry: first RTH close beyond the overnight range, with the break ────
    def _maybe_enter(self, ts: int, c: float) -> list[Order]:
        if self.on_hi is None or self.on_lo is None:
            return []                          # no overnight captured -> stand down
        rng = self.on_hi - self.on_lo
        if rng < self.min_range:
            return []                          # too quiet a night to mean anything
        buf = self.buf_frac * rng
        if c > self.on_hi + buf and 1 not in self.used:
            side = 1
        elif c < self.on_lo - buf and -1 not in self.used:
            side = -1
        else:
            return []
        if not self.gamma_entry_ok(ts, "short"):     # continuation needs short gamma
            return []
        self.used.add(side)
        stop = max(self.stop_floor, self.stop_mult * rng)
        trail = max(self.trail_floor, self.trail_mult * rng)
        self.trade = {"side": side, "entry": c, "stop": stop, "trail": trail, "peak": 0.0}
        return [Order(self.symbol, side, 1, tag="entry-onbreak")]

    # ── management: failed break (back inside), else hard/trailing stop ──────
    def _manage(self, c: float) -> list[Order]:
        t = self.trade
        side = t["side"]
        # closed back INSIDE the overnight range -> the break failed
        if (side > 0 and c < self.on_hi) or (side < 0 and c > self.on_lo):
            return self._flatten("range-fail")
        fe = (c - t["entry"]) * side
        t["peak"] = max(t["peak"], fe)
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
        return [Order(self.symbol, -side, qty, tag=f"onb-{why}", reduce_only=True)]


__all__ = ["OvernightBreakStrategy"]
