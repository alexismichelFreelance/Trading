"""MacroDipStrategy — buy a 3-day dip. The macro gate is OFF by default,
because BUILDING IT DISPROVED IT.

THE PLAIN DIP (what this ships, verified by driving this strategy object over
ES daily 2011-2026):
    170 independent trades   +3,031 ES points   +17.8/trade   median +20.5
    hit 62%   14 of 16 years positive   top 3 trades = 34%, +2,010 without them
    ~11 trades a year. Analysis on the same rule: 184 trades, +18.9/trade.
The sleeve reproduces the analysis, which is the bar.

THE MACRO GATE (10y yield AND 10y breakeven both below their trailing 250-day
medians) LOOKED like a switch and was not:
    per DIP DAY      gate ON n=158 +1.25% / gate OFF n=244 +0.24%
    per INDEPENDENT  gate ON n= 69 +0.94% / gate OFF n=118 +0.29%
The first line counts consecutive days inside one dip as separate observations.
You are already long on those days, so the engine can only take the second. The
gate does still discriminate per trade (22.9 vs 13.5 points) -- but on 69 trades
in 16 years with the top 3 carrying 50% of the P&L, and moving the percentile by
a single rank swings it from +1,581 to +311 points. That is not an edge, it is
an implementation detail.

**The general lesson, and it is the third time this session: a result counted
per DAY when the trade lasts five days is inflated by overlap. Count trades.**

DIP = the 3-day return at or below its own TRAILING 250-day 10th percentile, so
a "dip" rescales with the regime instead of being a fixed percentage.
NO STOP, like ibs and rsi2 -- stops break daily mean reversion. Sizing is the
risk control and this sleeve is gap-exposed overnight in the same risk class.
CAVEAT: long-only equity dip-buying across a 16-year bull market, so some of
this is beta.
"""
from __future__ import annotations

import json
import logging
from collections import deque
from datetime import date, timedelta
from pathlib import Path

from ..core.events import Bar
from ..core.orders import Order
from ..core.timeutil import et_minute_of_day, et_session_date
from .base import BaseStrategy

log = logging.getLogger("engine.macrodip")

DECISION_MIN = 15 * 60 + 59        # 15:59 ET
LOOKBACK = 3                       # the dip is a 3-day return
PCTL_WIN = 250                     # trailing window for the dip threshold
PCTL = 0.10
HOLD_DAYS = 5
MAX_STALE_DAYS = 6                 # a gate older than this stands the sleeve down


def _load_seed(seed_path, symbol: str) -> list[tuple[str, float]]:
    """Historical daily closes, so the TRAILING 250-day dip percentile is live
    from the first session. Without it the sleeve needs 253 sessions -- a year --
    before it can decide anything, and would sit inert while looking healthy."""
    from ..core.config import root_symbol
    if seed_path is None:
        seed_path = (Path(__file__).resolve().parents[2] / "config"
                     / f"macrodip_seed_{root_symbol(symbol)}.json")
    p = Path(seed_path)
    if not p.exists():
        log.error("MACRODIP SEED MISSING at %s -- the 250-day dip percentile "
                  "needs a year to warm from cold, so this sleeve will NOT "
                  "trade. Regenerate it before relying on this sleeve.", p)
        return []
    try:
        d = json.loads(p.read_text())
        return [(r["day"], float(r["c"])) for r in d["closes"]]
    except Exception as ex:                       # pragma: no cover - config error
        log.error("MACRODIP SEED UNREADABLE at %s: %s", p, ex)
        return []


def _load_gate(path: str | Path | None) -> dict:
    p = Path(path) if path else (Path(__file__).resolve().parents[2]
                                 / "config" / "macro_gate.json")
    if not p.exists():
        log.error("MACRO GATE MISSING at %s -- run tools/fetch_macro.py. "
                  "This sleeve will not trade.", p)
        return {}
    try:
        return json.loads(p.read_text())
    except Exception as ex:                       # pragma: no cover - config error
        log.error("MACRO GATE UNREADABLE at %s: %s", p, ex)
        return {}


class MacroDipStrategy(BaseStrategy):
    holds_overnight = True

    def __init__(self, symbol: str, *, qty: int = 1, hold_days: int = HOLD_DAYS,
                 gate_path: str | Path | None = None,
                 gate: dict | None = None,
                 seed: list[tuple[str, float]] | None = None,
                 seed_path: str | Path | None = None,
                 require_gate: bool = False) -> None:
        self.symbol = symbol
        self.qty = qty
        self.hold_days = hold_days
        # The gate is opt-in: it did not survive being counted per TRADE.
        self.require_gate = require_gate
        self._gate = gate if gate is not None else _load_gate(gate_path)
        self._closes: deque[float] = deque(maxlen=PCTL_WIN + LOOKBACK + 2)
        self._days: deque[str] = deque(maxlen=PCTL_WIN + LOOKBACK + 2)
        if seed is None:
            seed = _load_seed(seed_path, symbol)
        for d, c in (seed or []):
            self._days.append(d); self._closes.append(float(c))
        self._day: str | None = None
        self._c: float | None = None
        self._decided = False
        self._held = 0
        self.pos = 0

    def on_position(self, p) -> None:
        self.pos = p.qty

    def reset_for_live(self) -> None:
        self.pos = 0
        self._held = 0

    # ── the dip test, from closed prior days plus today's close ─────────────
    def _dip(self) -> bool | None:
        c = list(self._closes)
        if len(c) < PCTL_WIN + LOOKBACK + 1:
            return None
        r3 = [(c[i] / c[i - LOOKBACK] - 1.0) for i in range(LOOKBACK, len(c))]
        today = r3[-1]
        hist = sorted(r3[-(PCTL_WIN + 1):-1])      # TRAILING only, excludes today
        k = max(0, int(len(hist) * PCTL) - 1)
        return today <= hist[k]

    def gate_on(self, day: str) -> bool | None:
        """True/False, or None when the cache is missing or too stale to trust."""
        if not self.require_gate:
            return True
        g = self._gate.get("gate") or {}
        if not g:
            return None
        asof = self._gate.get("asof")
        try:
            d0 = date.fromisoformat(day); d1 = date.fromisoformat(asof)
        except (TypeError, ValueError):
            return None
        if (d0 - d1).days > MAX_STALE_DAYS:
            return None                            # stale: stand down, do not assume
        prior = [k for k in g if k <= day]
        if not prior:
            return None
        return bool(g[max(prior)])

    def on_bar(self, b: Bar) -> list[Order]:
        if b.tf != "1m":
            return []
        day = et_session_date(b.ts)
        if day != self._day:
            if self._day is not None and self._c is not None:
                if not self._days or self._days[-1] != self._day:
                    self._days.append(self._day)
                    self._closes.append(self._c)
                if self.pos != 0:
                    self._held += 1
            self._day = day
            self._decided = False
        self._c = b.c
        if self._decided or et_minute_of_day(b.ts) < DECISION_MIN:
            return []
        self._decided = True
        # today's close participates in the decision but is appended at the roll
        closes_with_today = list(self._closes) + [b.c]
        saved = self._closes
        self._closes = deque(closes_with_today, maxlen=saved.maxlen)
        try:
            dip = self._dip()
        finally:
            self._closes = saved
        if self.pos != 0:
            if self._held >= self.hold_days:
                self._held = 0
                return [Order(self.symbol, -1 if self.pos > 0 else 1,
                              abs(self.pos), tag="macrodip-exit", reduce_only=True)]
            return []
        if dip is None:
            return []
        on = self.gate_on(day)
        if on is None:
            log.warning("macrodip %s: macro gate unavailable or stale; standing down",
                        self.symbol)
            return []
        if dip and on:
            self._held = 0
            return [Order(self.symbol, 1, self.qty, tag="macrodip-entry")]
        return []
