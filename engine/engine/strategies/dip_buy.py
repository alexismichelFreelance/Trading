"""DipBuyStrategy — the user-modeled long-gamma mean-reversion sleeve.

Mechanizes the user's demonstrated edge (BAND_RETEST_STUDY.md, decoded from their
2026-07-08 and 07-10 wins): after an intraday flush, buy the first touch of a
lower VWAP band (VWAP-2σ) or a band-confluent prior-day close, scale half at +4,
run the rest to VWAP. Shorts are mirrored on the upper band. Causal: session VWAP
and σ are cumulative from the RTH open; the flush window looks only backwards.

Regime is a STRATEGY choice: pass gamma=GammaRegime() (the dipbuy_gex variant)
and entries stand down on short-gamma days (gexp_prev <= 1/3 — dips amplify
there; DAY_SELECTION.md). Raw (gamma=None) fires regardless. Size caps stay in
the RiskSupervisor. Consumes 1m bars directly (no aggregation).

NOT a validated edge yet: the 2026 band-retest sample was small and contaminated
by these very anchor days; this sleeve deploys OFF by default and rides the
late-Aug forward test. The value now is a clean, anchor-tested mechanism.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ..core.events import Bar
from ..core.orders import Order
from ..core.timeutil import et_minute_of_day, et_session_date
from .base import BaseStrategy, level_fill
from .sizing import position_size

RTH_START, RTH_END = 570, 960          # ET minutes
SCALP = 4.0
STOP_BUF = 6.0
FLUSH_PTS = 15.0                       # min decline to arm a dip
FLUSH_WIN = 90                         # bars (minutes) lookback
FLUSH_AGE = 10                         # low must be >= this many bars old
CONF = 3.0                             # prior-close/band confluence tolerance
TIME_STOP = 120                        # bars
MOC_MIN = 959                          # flatten at 15:59 ET
RISK = 1000.0                          # moderate engagement (DAY_SELECTION.md)
SIZE_CAP = 5


@dataclass
class _Pos:
    dir: int
    entry: float
    stop: float
    runner_tgt: float
    size: int
    remaining: int
    scalp_px: float
    scalped: bool = False
    age: int = 0


class DipBuyStrategy(BaseStrategy):
    def __init__(self, symbol: str, point_usd: float = 50.0, gamma=None) -> None:
        self.symbol = symbol
        self.point_usd = point_usd     # $/pt for sizing (from InstrumentSpec)
        # optional GammaRegime: entries only on mid/LONG-gamma days (dealers pin,
        # dips revert — the user's regime). A strategy choice, not an engine gate.
        self.gamma = gamma
        self.pos = 0
        self.trade: _Pos | None = None
        self._day: str | None = None
        self._prev_close: float | None = None      # prior session RTH close
        self._last_close: float | None = None       # rolling last RTH close (this session)
        self._reset_session()

    def _reset_session(self) -> None:
        self.cum_v = 0.0
        self.cum_pv = 0.0
        self.cum_d2 = 0.0
        self.his: deque = deque(maxlen=FLUSH_WIN)    # (high, low)
        self.since_touch_lo: deque = deque(maxlen=16)  # recent lows (prior-clear check)
        self.since_touch_hi: deque = deque(maxlen=16)
        self._cool = 0                               # bars until re-entry allowed

    def on_position(self, p) -> None:
        self.pos = p.qty

    def reset_for_live(self) -> None:
        self.trade = None
        self.pos = 0

    def on_bar(self, bar: Bar) -> list[Order]:
        m = et_minute_of_day(bar.ts)
        if not (RTH_START <= m < RTH_END):
            return []
        day = et_session_date(bar.ts)
        if day != self._day:                         # new RTH session
            if self._last_close is not None:
                self._prev_close = self._last_close
            self._day = day
            self._reset_session()
        # cumulative session VWAP + σ (causal)
        v = float(bar.v) if bar.v else 0.0
        self.cum_v += v
        self.cum_pv += bar.c * v
        vwap = self.cum_pv / self.cum_v if self.cum_v > 0 else bar.c
        self.cum_d2 += v * (bar.c - vwap) ** 2
        sigma = (self.cum_d2 / self.cum_v) ** 0.5 if self.cum_v > 0 else 0.0
        self._last_close = bar.c

        orders: list[Order] = []
        if self.trade is not None:
            orders += self._manage(bar, vwap, m)
        elif self.pos == 0 and self._cool <= 0:
            orders += self._scan(bar, vwap, sigma, m)
        if self._cool > 0:
            self._cool -= 1
        self.his.append((bar.h, bar.l))
        self.since_touch_lo.append(bar.l)
        self.since_touch_hi.append(bar.h)
        return orders

    # ── entry ────────────────────────────────────────────────────────────
    def _scan(self, bar: Bar, vwap: float, sigma: float, m: int) -> list[Order]:
        if len(self.his) < 40 or m >= MOC_MIN - TIME_STOP // 2:
            return []
        if not self.gamma_entry_ok(bar.ts, "long"):
            return []                    # short-gamma day: dips amplify, stand down
        highs = [h for h, _ in self.his]
        lows = [l for _, l in self.his]
        hh, ll = max(highs), min(lows)
        li = max(range(len(lows)), key=lambda i: -lows[i])   # index of min low
        hi = max(range(len(highs)), key=lambda i: highs[i])  # index of max high
        n = len(self.his)
        long_ok = (hh - ll >= FLUSH_PTS) and (n - 1 - li >= FLUSH_AGE) and bar.c > ll
        short_ok = (hh - ll >= FLUSH_PTS) and (n - 1 - hi >= FLUSH_AGE) and bar.c < hh

        for dset, dsgn in (("A", +1), ("B", +1), ("A", -1), ("B", -1)):
            if dsgn > 0 and not long_ok:
                continue
            if dsgn < 0 and not short_ok:
                continue
            if dset == "A":
                level = vwap - dsgn * 2 * sigma
            else:
                if self._prev_close is None:
                    continue
                b1 = vwap - dsgn * sigma
                b2 = vwap - dsgn * 2 * sigma
                if min(abs(self._prev_close - b1), abs(self._prev_close - b2)) > CONF:
                    continue
                level = self._prev_close
            touched = bar.l <= level <= bar.h
            prior_clear = (all(x > level for x in self.since_touch_lo) if dsgn > 0
                           else all(x < level for x in self.since_touch_hi))
            if touched and prior_clear:
                return self._enter(dsgn, level, vwap, dset)
        return []

    def _enter(self, d: int, level: float, vwap: float, dset: str) -> list[Order]:
        stop = level - d * STOP_BUF
        size = position_size(RISK, STOP_BUF, self.point_usd, SIZE_CAP)
        if size <= 0:
            return []
        self.trade = _Pos(dir=d, entry=level, stop=stop, runner_tgt=vwap, size=size,
                          remaining=size, scalp_px=level + d * SCALP)
        return [Order(self.symbol, d, size, tag=f"dip{dset}-entry")]

    # ── management ───────────────────────────────────────────────────────
    def _manage(self, bar: Bar, vwap: float, m: int) -> list[Order]:
        t = self.trade
        assert t is not None
        d = t.dir
        t.age += 1

        def close(qty: int, tag: str, done: bool,
                  at: float | None = None) -> list[Order]:
            qty = min(qty, t.remaining)
            if done:
                self.trade = None
                self._cool = 60                       # 1h cooldown, like the study
            return [Order(self.symbol, -d, qty, tag=tag, reduce_only=True,
                          trigger_price=at)] if qty > 0 else []

        # MOC / time stop have no level -- they really are at the market.
        if m >= MOC_MIN or t.age >= TIME_STOP:
            return close(t.remaining, "dip-moc", done=True)
        stop_hit = (bar.l <= t.stop) if d > 0 else (bar.h >= t.stop)
        if stop_hit:
            return close(t.remaining, "dip-stop", done=True,
                         at=level_fill(bar, t.stop, rising=d < 0))
        if not t.scalped:
            scalp_hit = (bar.h >= t.scalp_px) if d > 0 else (bar.l <= t.scalp_px)
            if scalp_hit:
                px = level_fill(bar, t.scalp_px, rising=d > 0)
                t.scalped = True
                t.stop = t.entry                      # runner to breakeven
                half = t.size // 2
                if half > 0:
                    t.remaining -= half
                    return close(half, "dip-scale", done=(t.remaining <= 0), at=px)
        else:
            run_hit = (bar.h >= t.runner_tgt) if d > 0 else (bar.l <= t.runner_tgt)
            if run_hit:
                return close(t.remaining, "dip-vwap", done=True,
                             at=level_fill(bar, t.runner_tgt, rising=d > 0))
        return []


__all__ = ["DipBuyStrategy"]
