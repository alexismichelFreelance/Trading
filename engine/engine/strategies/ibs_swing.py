"""IBSSwingStrategy — daily IBS mean-reversion (sleeve #5, SWING: holds overnight).

At the session's last minute (15:59 ET decision bar), compute the day's
IBS = (C - L) / (H - L) from the session H/L/C it has streamed:
  - flat  and IBS < in_th  -> BUY  qty at MOC
  - long  and IBS > out_th -> SELL all at MOC
No intraday stop (classic spec — stops break daily mean reversion; the tail
risk is documented in ibs_oracle.py and must be handled by SIZING).

Session span note: in RTH-only replay the IBS is RTH IBS (SPY-like variant,
t=4.4); on a ~23h live futures feed it approximates the ES=F 24h variant
(t=4.2). Both validated; see strategy_lab/QUIET_CLASSICS_SCREEN.md.

Optional GEX sizing (mechanism-validated, see gamma/GEX_FINDINGS.md): entries
in LONG-gamma regimes historically win 78% @ +14.9/trade vs 66% @ +7.5 in
SHORT-gamma (with 3x the tail). With gamma=GammaRegime(), qty doubles when
gexp_prev >= gex_full_pctl; base qty otherwise. Default OFF (qty=qty_base).
"""
from __future__ import annotations

from ..core.events import Bar, Fill
from ..core.orders import Order
from ..core.timeutil import et, et_session_date
from .base import BaseStrategy

DECISION_MIN = 15 * 60 + 59      # 15:59 ET


class IBSSwingStrategy(BaseStrategy):
    # A SWING sleeve by design: it is meant to carry positions through the
    # close. The only sleeve entitled to opt out of the session flat.
    holds_overnight = True

    # out_th 0.90, not the classic 0.80. Measured on the same 486 entries over
    # 16y (strategy_lab/ibs_exits.py): +7,106pt vs +5,953, +18.41/trade vs
    # +12.30, t 5.4 vs 5.0, win 72% vs 68%, and BOTH tails smaller -- worst
    # trade -269 vs -321, max drawdown -302 vs -386 (ret/DD 23.5 vs 15.4).
    # Monotone from 0.5 to 0.9 and an interior optimum: 0.95 is worse (21.7),
    # 0.99 collapses (10.0, DD -800). Beats 0.80 in H1, H2 AND the out-of-sample
    # year separately. Not merely "hold longer": a 5-day time exit at the same
    # mean hold returns +13.14/trade at ret/DD 7.8.
    def __init__(self, symbol: str, *, in_th: float = 0.20, out_th: float = 0.90,
                 qty_base: int = 1, gamma=None, gex_full_pctl: float = 1.0 / 3.0,
                 qty_gex_boost: int = 2) -> None:
        self.symbol = symbol
        self.in_th, self.out_th = in_th, out_th
        self.qty_base = qty_base
        self.gamma = gamma                      # optional GammaRegime
        self.gex_full_pctl = gex_full_pctl
        self.qty_gex_boost = qty_gex_boost
        self._day: str | None = None
        self._h = self._l = self._c = None
        self._decided = False
        self.pos = 0

    def _entry_qty(self, day: str) -> int:
        if self.gamma is None:
            return self.qty_base
        gp = self.gamma.gexp_prev(day)
        if gp is not None and gp >= self.gex_full_pctl:
            return self.qty_gex_boost           # long-gamma regime: size up
        return self.qty_base

    def on_bar(self, b: Bar) -> list[Order]:
        if b.tf != "1m":
            return []
        day = et_session_date(b.ts)
        if day != self._day:
            self._day = day
            self._h, self._l, self._c = b.h, b.l, b.c
            self._decided = False
        else:
            self._h = max(self._h, b.h)
            self._l = min(self._l, b.l)
            self._c = b.c
        t = et(b.ts)
        mod = t.hour * 60 + t.minute
        if mod < DECISION_MIN or self._decided or self._h <= self._l:
            return []
        self._decided = True
        ibs = (self._c - self._l) / (self._h - self._l)
        if self.pos == 0 and ibs < self.in_th:
            return [Order(self.symbol, 1, self._entry_qty(day), tag="ibs-entry")]
        if self.pos > 0 and ibs > self.out_th:
            return [Order(self.symbol, -1, self.pos, tag="ibs-exit", reduce_only=True)]
        return []

    def on_position(self, p) -> None:
        self.pos = p.qty

    def reset_for_live(self) -> None:
        # clear phantom position; keep `_decided` (the 15:59 decision is
        # time-gated — don't re-decide stale if it passed in the backfill)
        self.pos = 0

    def restore_state(self, pos: int, avg_px: float) -> bool:
        # IBS holds overnight by design and its exit is IBS-threshold based
        # (not price-based), so the net position is all it needs to resume
        # managing a position carried across a restart. avg_px is unused here.
        self.pos = pos
        return True

    def on_fill(self, f: Fill) -> None:
        return None


__all__ = ["IBSSwingStrategy"]
