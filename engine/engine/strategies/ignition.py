"""Ignition strategy (STRATEGY_SPEC.md).

Detect an order-flow ignition aligned with the short-term trend, enter at pxc,
then let the 1-hour HMM regime gate decide the exit:
  - TREND hour: ride to the nearest opposing VIRGIN 30m zone (fallback pivot),
                opposite-side pivot stop, -12pt cap, 600s horizon.
  - CHOP  hour: BOOK-healing exit (leading-side resting book rebuilt after a
                profit peak), -4pt hard stop, 600s horizon.

Entry per second: str>=5 & avol>=800 & book-confirm(dir) & trend-aligned.
One contract (parity is reported in points; the flat 0.517 round-turn cost is
applied at reporting). Strictly causal: every feature is incremental.
"""
from __future__ import annotations

from collections import deque

from ..core.events import Bar, BookFlow, Trade
from ..core.orders import Order, OrderType
from ..core.timeutil import et_session_date, ns_to_utc, utc_hour
from ..features.bars import BarAggregator
from ..features.efficiency import OnlineKaufmanER, RollingEfficiency
from ..features.hmm import GaussianHMM2
from ..features.online import IgnitionFeatures
from ..features.pivots import SessionLevels
from ..features.zones import ZoneBook, ZoneDetector
from .base import BaseStrategy


class IgnitionStrategy(BaseStrategy):
    def __init__(self, symbol: str, hmm_path: str, *, str_min: float = 5.0,
                 avol_min: float = 800.0, trend_lag: int = 300, book_act: float = 2.0,
                 book_net: float = 200.0, book_giveback: float = 0.8,
                 pivot_min_dist: float = 2.0, horizon_s: float = 600.0,
                 chop_stop: float = 4.0, trend_cap: float = 12.0,
                 round_step: float = 50.0, book_min_hold: float = 8.0,
                 regime_states: dict | None = None, regime_mode: str = "states",
                 er_window_s: int = 7200, er_threshold: float = 0.70,
                 exit_mode: str = "fixed", trail_init: float = 6.0,
                 trail_width: float = 10.0,
                 gate_utc: tuple[int, int] | None = (13, 21),
                 gamma=None) -> None:
        self.symbol = symbol
        # NEW ENTRIES only inside the validated 13-21 UTC window (live feeds run
        # ~23h; the edge was researched on this window). Exits always run.
        self.gate_utc = gate_utc
        # optional GammaRegime: entries only on short-gamma days (trend earns
        # there — gamma/GEX_FINDINGS.md D). A strategy choice, not an engine gate.
        self.gamma = gamma
        self.feats = IgnitionFeatures(trend_lag=trend_lag)
        self.agg = BarAggregator(("30m", "1h"))
        self.zdet = ZoneDetector()
        self.zbook = ZoneBook()
        self.levels = SessionLevels(round_step)
        self.er = OnlineKaufmanER(3)
        self.hmm = GaussianHMM2.load(hmm_path)
        # regime source:
        #   "rolling_er" — CAUSAL trailing-window efficiency threshold (live-safe, recommended)
        #   "states"     — frozen per-hour states (replay parity; has intra-hour look-ahead)
        #   "hmm"        — online 1h-HMM forward-filter (causal but bar-close-lagged)
        self.regime_mode = regime_mode
        self.regime_states = regime_states
        self.er_threshold = er_threshold
        self.reff = RollingEfficiency(er_window_s, 3) if regime_mode == "rolling_er" else None
        self._er: float | None = None
        self._day = None
        # exit_mode="trailing" replaces the regime-gated fixed exits with a single
        # uniform trailing stop (initial hard stop trail_init, trail_width behind
        # the peak) — no regime, no target, no look-ahead. Beats the regime gate.
        self.exit_mode = exit_mode
        self.trail_init = trail_init
        self.trail_width = trail_width
        # params
        self.str_min, self.avol_min = str_min, avol_min
        self.book_act, self.book_net, self.book_giveback = book_act, book_net, book_giveback
        self.book_min_hold = book_min_hold
        self.pivot_min_dist = pivot_min_dist
        self.horizon_s, self.chop_stop, self.trend_cap = horizon_s, chop_stop, trend_cap
        # rolling-10s book net (add - cancel) per side
        self._bid_net: deque[int] = deque(maxlen=10)
        self._ask_net: deque[int] = deque(maxlen=10)
        # position / trade state
        self.pos = 0
        self._reset_trade()

    def _reset_trade(self) -> None:
        self.side = 0
        self.entry_px = None
        self.entry_ts = None
        self.regime = None
        self.peak_fe = 0.0
        self.target_px = None
        self.stop_px = None

    # ── coarse features from 1m bars ─────────────────────────────────────
    def on_bar(self, bar: Bar) -> list[Order]:
        self.levels.update_bar(bar)
        for hb in self.agg.update(bar):
            if hb.tf == "30m":
                for z in self.zdet.update(hb):     # update() returns a list of zones
                    self.zbook.add(z)
                self.zbook.on_bar(hb)
            elif hb.tf == "1h" and self.regime_states is None:
                # online causal filter (live path); per-day ER reset
                day = et_session_date(hb.ts)
                if day != self._day:
                    self._day = day
                    self.er = OnlineKaufmanER(3)
                erv = self.er.update(hb.c)
                if erv is not None:
                    self.hmm.update(erv, day)
        return []

    # ── per-second flow ──────────────────────────────────────────────────
    def on_trade(self, t: Trade) -> list[Order]:
        self.feats.add_trade(t)
        return []

    def on_bookflow(self, bf: BookFlow) -> list[Order]:
        self.feats.close_second(bf)
        self._bid_net.append(bf.bid_add - bf.bid_cancel)
        self._ask_net.append(bf.ask_add - bf.ask_cancel)
        px = self.feats.pxc
        if px is None:
            return []
        self.zbook.on_price(px, bf.ts)
        if self.reff is not None:
            self._er = self.reff.update(bf.ts, px, et_session_date(bf.ts))
        if self.pos != 0:
            return self._manage(px, bf.ts)
        return self._maybe_enter(px, bf.ts)

    def on_position(self, p) -> None:
        self.pos = p.qty

    # ── logic ────────────────────────────────────────────────────────────
    def _maybe_enter(self, px: float, ts: int) -> list[Order]:
        if self.gate_utc is not None and \
                not (self.gate_utc[0] <= ns_to_utc(ts).hour < self.gate_utc[1]):
            return []
        if not self.gamma_entry_ok(ts, "short"):
            return []
        f = self.feats
        if f.strength is None:
            return []
        d = f.dir
        if d == 0:
            return []
        if not (f.strength >= self.str_min and f.avol >= self.avol_min):
            return []
        if not f.book_confirm(d):
            return []
        if f.trend_sign() != d:                      # trend-aligned only
            return []
        # ENTER at pxc in direction d
        self.side = d
        self.entry_px = px
        self.entry_ts = ts
        self.peak_fe = 0.0
        if self.exit_mode == "trailing":
            self.regime = "trailing"
            self.target_px = self.stop_px = None
        else:
            if self.regime_mode == "rolling_er":
                is_trend = self._er is not None and self._er >= self.er_threshold
            elif self.regime_states is not None:
                is_trend = self.regime_states.get(utc_hour(ts), 0) == 1
            else:
                is_trend = self.hmm.is_trend()
            self.regime = "trend" if is_trend else "chop"
            if self.regime == "trend":
                z = self.zbook.nearest_opposing(px, d, self.pivot_min_dist)
                self.target_px = z.proximal() if z is not None else \
                    self.levels.target(px, d, self.pivot_min_dist)
                self.stop_px = self.levels.stop(px, d)
            else:
                self.target_px = self.stop_px = None
        return [Order(self.symbol, d, 1, OrderType.MARKET, tag=f"entry-{self.regime}")]

    def _manage(self, px: float, ts: int) -> list[Order]:
        side = self.side
        fe = (px - self.entry_px) * side
        self.peak_fe = max(self.peak_fe, fe)
        held = (ts - self.entry_ts) / 1e9
        tag = None
        if held >= self.horizon_s:
            tag = "horizon"
        elif self.exit_mode == "trailing":
            stop_fe = max(-self.trail_init, self.peak_fe - self.trail_width)
            if fe <= stop_fe:
                tag = "trail"
        elif self.regime == "trend":
            if fe <= -self.trend_cap:
                tag = "cap"
            elif self.target_px is not None and (
                    (side > 0 and px >= self.target_px) or (side < 0 and px <= self.target_px)):
                tag = "target"
            elif self.stop_px is not None and (
                    (side > 0 and px <= self.stop_px) or (side < 0 and px >= self.stop_px)):
                tag = "stop"
        else:  # chop
            if fe <= -self.chop_stop:
                tag = "chopstop"
            elif self.peak_fe >= self.book_act and held >= self.book_min_hold:
                lead = sum(self._ask_net) if side > 0 else sum(self._bid_net)
                if lead > self.book_net and fe <= self.book_giveback * self.peak_fe:
                    tag = "book"
        if tag is not None:
            self._reset_trade()
            return [Order(self.symbol, -side, 1, OrderType.MARKET, tag=tag, reduce_only=True)]
        return []


__all__ = ["IgnitionStrategy"]
