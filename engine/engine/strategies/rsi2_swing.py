"""RSI2SwingStrategy — Connors RSI(2) daily mean reversion (SWING: holds overnight).

The second sleeve in the book with an independent 16-year record, and the only
one found that improves IBS rather than diluting it.

Rule (published spec, nothing fitted here):
    at the 15:59 ET decision bar, on DAILY closes
      flat and RSI(2) < 10 and close > 200-day MA  -> BUY at MOC
      long and close > 5-day MA                    -> SELL all at MOC
The 200dMA filter belongs to THIS sleeve, not to IBS: on IBS it removes the
crash-rebound entries that pay for the losses (QUIET_CLASSICS_SCREEN.md), but
RSI(2) without it is a different, worse trade (16y: t=+3.1, ret/DD 3.83 unfiltered
vs t=+3.5, ret/DD 5.96 filtered).

Measured on SPX daily 2010-2026, net 0.517pt/RT (strategy_lab/ibs_verify.py
family): 366 trades, +2,875pt, +7.86/trade, t=+3.5, 75% win, 3.0-day hold, 26%
exposure, max drawdown -482pt.

Standalone it is much weaker than IBS (+7,118pt, t=+5.4, ret/DD 23.54) and is NOT
worth running alone. Its value is as a 25% satellite: despite being long on 71% of
its days while IBS is also long, daily-P&L correlation is only +0.47, and
IBS 75% / RSI2 25% lifts ret/DD from 8.99 to 10.56 -- an improvement that cannot
come from position scaling, since ret/DD is scale-invariant. Better in H2
(6.37->7.30) and out-of-sample (1.88->2.17), flat in H1 (5.64->5.54).

Exit is `close > 5-day MA`, not the conventional RSI(2)>70: same entries, +2,875
vs +2,512pt, t=+3.5 vs +2.7, drawdown -482 vs -698.

Like IBS this runs NO STOP -- stops break daily mean reversion. Sizing is the
risk control, and this sleeve is gap-exposed overnight in the same risk class.
"""
from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path

from ..core.events import Bar, Fill
from ..core.orders import Order
from ..core.timeutil import et, et_session_date
from .base import BaseStrategy

log = logging.getLogger("engine.rsi2")

DECISION_MIN = 15 * 60 + 59      # 15:59 ET
RSI_N = 2
MA_FAST = 5
MA_SLOW = 200


def _load_seed(seed_path, symbol: str) -> list[tuple[str, float]]:
    """Historical daily closes so the 200d MA is live from the first session.

    Missing seed is NOT silently tolerated: the sleeve would look healthy and
    never trade. It logs loudly and returns empty, which the warmup check below
    reports on the first decision bar.
    """
    from ..core.config import root_symbol
    if seed_path is None:
        root = root_symbol(symbol)
        seed_path = Path(__file__).resolve().parents[2] / "config" / f"rsi2_seed_{root}.json"
    p = Path(seed_path)
    if not p.exists():
        log.error("RSI2 SEED MISSING at %s -- the 200d MA needs 200 sessions to "
                  "warm from cold, so this sleeve will NOT trade for a year. "
                  "Regenerate it before relying on this sleeve.", p)
        return []
    try:
        d = json.loads(p.read_text())
        return [(r["day"], float(r["c"])) for r in d["closes"]]
    except Exception as ex:                     # pragma: no cover - config error
        log.error("RSI2 SEED UNREADABLE at %s: %s", p, ex)
        return []


class RSI2SwingStrategy(BaseStrategy):
    # Holds through the close by design, like IBS. Entitled to opt out of the
    # engine's session flat.
    holds_overnight = True

    def __init__(self, symbol: str, *, rsi_in: float = 10.0,
                 qty_base: int = 1, ma_fast: int = MA_FAST,
                 ma_slow: int = MA_SLOW, seed_path: str | Path | None = None,
                 seed: list[tuple[str, float]] | None = None) -> None:
        self.symbol = symbol
        self.rsi_in = rsi_in
        self.qty_base = qty_base
        self.ma_fast, self.ma_slow = ma_fast, ma_slow
        # daily closes, newest last. maxlen bounds memory on a long-running
        # engine -- only the slow MA and a 2-period RSI are ever needed.
        self._closes: deque[float] = deque(maxlen=ma_slow + 2)
        # SEEDING. Without it this sleeve accumulates one close per session from
        # a cold start and its 200-day MA returns None for 200 SESSIONS -- it
        # would sit inert for a year. The seed is a file of historical daily
        # closes for THIS instrument (config/rsi2_seed_ES.json, ES=F). Entries
        # dated on or after the first live session are dropped, so the live
        # feed always owns the current day and nothing is counted twice.
        self._seed: list[tuple[str, float]] = list(seed or [])
        if seed is None:
            self._seed = _load_seed(seed_path, symbol)
        self._seeded = False
        self._day: str | None = None
        self._c: float | None = None
        self._decided = False
        self.pos = 0

    # ── indicators (computed only from CLOSED prior days + today's close) ────
    def _rsi2(self) -> float | None:
        if len(self._closes) < RSI_N + 1:
            return None
        c = list(self._closes)
        gains = losses = 0.0
        for a, b in zip(c[-(RSI_N + 1):-1], c[-RSI_N:]):
            d = b - a
            gains += max(d, 0.0)
            losses += max(-d, 0.0)
        if losses == 0:
            return 100.0
        rs = (gains / RSI_N) / (losses / RSI_N)
        return 100.0 - 100.0 / (1.0 + rs)

    def _ma(self, n: int) -> float | None:
        if len(self._closes) < n:
            return None
        c = list(self._closes)[-n:]
        return sum(c) / n

    def on_bar(self, b: Bar) -> list[Order]:
        if b.tf != "1m":
            return []
        day = et_session_date(b.ts)
        if not self._seeded:
            # drop any seed row dated on/after the first live session so the
            # feed owns the current day and nothing is double counted
            kept = [c for d, c in self._seed if d < day]
            for c in kept[-(self.ma_slow + 1):]:
                self._closes.append(c)
            self._seeded = True
            log.info("RSI2 seeded with %d historical closes (through %s); "
                     "slow MA ready=%s", len(self._closes),
                     kept[-1] if kept else "-",
                     len(self._closes) >= self.ma_slow)
        if day != self._day:
            # a new session began: the previous day's close is now final
            if self._c is not None:
                self._closes.append(self._c)
            self._day = day
            self._decided = False
        self._c = b.c
        t = et(b.ts)
        if t.hour * 60 + t.minute < DECISION_MIN or self._decided:
            return []
        self._decided = True

        # today's close participates in the indicators without being appended
        # twice -- it is appended at the NEXT session rollover.
        self._closes.append(self._c)
        try:
            r = self._rsi2()
            fast, slow = self._ma(self.ma_fast), self._ma(self.ma_slow)
        finally:
            self._closes.pop()

        if r is None or fast is None or slow is None:
            return []
        if self.pos == 0 and r < self.rsi_in and self._c > slow:
            return [Order(self.symbol, 1, self.qty_base, tag="rsi2-entry")]
        if self.pos > 0 and self._c > fast:
            return [Order(self.symbol, -1, self.pos, tag="rsi2-exit",
                          reduce_only=True)]
        return []

    def on_position(self, p) -> None:
        self.pos = p.qty

    def reset_for_live(self) -> None:
        # clear a phantom position; keep `_decided` (the 15:59 decision is
        # time-gated -- do not re-decide if it already passed during backfill)
        self.pos = 0

    def restore_state(self, pos: int, avg_px: float) -> bool:
        # The exit is indicator-based, not price-based, so the net position is
        # all that is needed to resume managing a carried position. avg_px unused.
        self.pos = pos
        return True

    def on_fill(self, f: Fill) -> None:
        return None


__all__ = ["RSI2SwingStrategy"]
