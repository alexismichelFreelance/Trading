"""RegimeGate — allocation gate keyed on the dealer-gamma regime.

Validated on Feb-May 2025 (gamma/GEX_FINDINGS.md, section D): the trend sleeves
(ignition, open-drive, flow) earn almost entirely on short-gamma days
(gexp_prev <= 1/3, dealers amplify); on long/mid-gamma days dealers pin and the
trend sleeves bleed. Gating them to short-gamma days kept 96% of portfolio P&L
at 27% less max drawdown on that window; its larger expected value is
out-of-window — it stops the trend sleeves from grinding through months of chop
(their known failure mode). The zones/structure sleeve (and IBS) are ungated.

This is a live ALLOCATION gate, distinct from the RiskSupervisor (safety). It
suppresses only NEW exposure from trend sleeves on non-short-gamma days; exits
and reduces always pass. Fail-open: if the regime is unknown for a day (no GEX
row yet) it ALLOWS and logs — the gate only ever removes trades it is confident
about. Off by default so replay/parity are untouched.
"""
from __future__ import annotations

import logging

from .timeutil import et_session_date

log = logging.getLogger("engine.regime")

# the trend sleeves this gate applies to (class names)
TREND_SLEEVES = ("IgnitionStrategy", "OpenDriveStrategy", "FlowFollowingStrategy")
# mean-reversion sleeves — the EXACT complement: on when trend is off
MR_SLEEVES = ("DipBuyStrategy",)


def increases_exposure(base: int, side: int, qty: int) -> bool:
    """True if side*qty grows |position| from `base` (an entry/add, not a reduce)."""
    return abs(base + side * qty) > abs(base)


class RegimeGate:
    def __init__(self, gamma, trend_sleeves=TREND_SLEEVES, mr_sleeves=MR_SLEEVES,
                 max_pctl: float = 1.0 / 3.0, enabled: bool = True) -> None:
        self.gamma = gamma                       # GammaRegime | None
        self.trend = set(trend_sleeves)
        self.mr = set(mr_sleeves)
        self.max_pctl = max_pctl                 # gexp_prev threshold; trend<=, MR>
        self.enabled = enabled
        self._logged_days: set[str] = set()      # log the day's regime once

    def blocks(self, name: str, base: int, side: int, qty: int, ts: int) -> bool:
        """True -> suppress this ENTRY. Trend sleeves are blocked on non-short-gamma
        days; mean-reversion sleeves are blocked on short-gamma days (the exact
        complement). Exits/reduces and ungated sleeves are never blocked; fail-open
        on unknown regime."""
        is_trend, is_mr = name in self.trend, name in self.mr
        if not self.enabled or not (is_trend or is_mr):
            return False
        if not increases_exposure(base, side, qty):
            return False                          # exits always allowed
        if self.gamma is None:
            return False
        day = et_session_date(ts)
        sg = self.gamma.is_short_gamma(day, self.max_pctl)
        if day not in self._logged_days:
            self._logged_days.add(day)
            v = self.gamma.gexp_prev(day)
            log.info("regime %s: gexp_prev=%s -> %s", day,
                     "n/a" if v is None else f"{v:.2f}",
                     "unknown (allow all)" if sg is None else
                     ("SHORT-gamma (trend ON / dip-buy OFF)" if sg
                      else "mid/long-gamma (trend OFF / dip-buy ON)"))
        if sg is None:
            return False                          # unknown -> allow
        return (sg is False) if is_trend else (sg is True)


__all__ = ["RegimeGate", "TREND_SLEEVES", "MR_SLEEVES", "increases_exposure"]
