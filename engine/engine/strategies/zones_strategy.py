"""ZoneLifecycleStrategy — event-driven, single-position (live/replay engine form).

Detects 30m S/D zones and trades the three setups (fade / break / flip) with the
half-off-at-+4 breakeven-runner scale-out. The SAME decision logic as
zones_oracle.py (the parity gate); this version runs one position at a time
through the engine and manages exits against each closed 30m bar, so its P&L is
the realistic single-position number (lower than the per-signal oracle's +$47.5k).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.events import Bar
from ..core.orders import Order
from ..core.timeutil import ns_to_utc
from ..features.bars import BarAggregator
from ..features.zones import ZoneDetector
from .base import BaseStrategy
from .sizing import position_size

SCALP = 4.0
K_BARS = 16
RISK = 2000.0


@dataclass
class _ZoneRec:
    k: int
    dir: int
    top: float
    bot: float
    fade_done: bool = False
    broke: bool = False
    break_k: int | None = None
    flip_done: bool = False
    ts: int = 0                    # creation time (ns) — metadata for chart painting

    @property
    def prox(self) -> float:
        return self.top if self.dir > 0 else self.bot


@dataclass
class _Trade:
    setup: str
    dir: int
    entry: float
    stop: float
    target: float
    size: int
    scalp_px: float
    scalped: bool = False
    remaining: int = 0
    bars_left: int = K_BARS


class ZoneLifecycleStrategy(BaseStrategy):
    def __init__(self, symbol: str,
                 gate_utc: tuple[int, int] | None = (13, 21)) -> None:
        self.symbol = symbol
        self.agg = BarAggregator(("30m",))
        self.det = ZoneDetector()
        self.zones: list[_ZoneRec] = []
        self._k = -1
        self.pos = 0
        self.trade: _Trade | None = None
        # NEW entries only inside the validated window; management always runs
        self.gate_utc = gate_utc

    def on_bar(self, bar: Bar) -> list[Order]:
        orders: list[Order] = []
        for b in self.agg.update(bar):
            orders += self._on_30m(b)
        return orders

    def on_position(self, p) -> None:
        self.pos = p.qty

    def reset_for_live(self) -> None:
        # keep detected zones (market structure), drop the phantom warmup trade
        # and RE-ARM every still-valid zone so live touches fade/break cleanly
        self.trade = None
        self.pos = 0
        for z in self.zones:
            if not z.broke:
                z.fade_done = False
                z.flip_done = False

    # ── per closed 30m bar ───────────────────────────────────────────────
    def _on_30m(self, b: Bar) -> list[Order]:
        self._k += 1
        orders: list[Order] = []
        if self.trade is not None:
            orders += self._manage(b)
        z = self.det.update(b)
        if z is not None:
            self.zones.append(_ZoneRec(self._k, z.direction, z.top, z.bot, ts=b.ts))
        in_window = self.gate_utc is None or \
            (self.gate_utc[0] <= ns_to_utc(b.ts).hour < self.gate_utc[1])
        if self.trade is None and self.pos == 0 and in_window:
            orders += self._scan(b)
        return orders

    def _opp_target(self, want_dir: int, price: float, before_k: int, dir_sign: int) -> float | None:
        cands = [zz for zz in self.zones if zz.dir == want_dir and zz.k < before_k
                 and (zz.bot > price + 3 if dir_sign > 0 else zz.top < price - 3)]
        if not cands:
            return None
        return min(z.bot for z in cands) if dir_sign > 0 else max(z.top for z in cands)

    def _enter(self, setup: str, d: int, entry: float, stop: float, target: float) -> list[Order]:
        risk = abs(entry - stop)
        size = position_size(RISK, risk, 50.0, 30)
        if size <= 0:
            return []
        self.trade = _Trade(setup, d, entry, stop, target, size,
                            entry + d * SCALP, remaining=size)
        return [Order(self.symbol, d, size, tag=f"{setup.lower()}-entry")]

    def _scan(self, b: Bar) -> list[Order]:
        for z in self.zones:
            d = z.dir
            # FADE — 1st touch of a fresh zone
            if not z.fade_done and not z.broke:
                touch = (b.l <= z.top and b.l >= z.bot - 0.5) if d > 0 else (b.h >= z.bot and b.h <= z.top + 0.5)
                if touch:
                    z.fade_done = True
                    tgt = self._opp_target(-d, z.prox, self._k, d)
                    if tgt is None:
                        stop0 = z.bot - 1 if d > 0 else z.top + 1
                        tgt = z.prox + d * abs(z.prox - stop0) * 2
                    return self._enter("FADE", d, z.prox, z.bot - 1 if d > 0 else z.top + 1, tgt)
            # detect break
            if not z.broke and ((b.c < z.bot - 1) if d > 0 else (b.c > z.top + 1)):
                z.broke = True
                z.break_k = self._k
                bdir = -d
                b_entry = b.c
                b_stop = z.top + 1 if d > 0 else z.bot - 1
                tgt = self._opp_target(d, b_entry, self._k, bdir)
                if tgt is None:
                    tgt = b_entry + bdir * abs(b_entry - b_stop) * 2
                return self._enter("BREAK", bdir, b_entry, b_stop, tgt)
            # FLIP — retest from broken side
            if z.broke and not z.flip_done and z.break_k is not None and self._k >= z.break_k + 2:
                ft = (b.h >= z.bot and b.h <= z.top + 0.5) if d > 0 else (b.l <= z.top and b.l >= z.bot - 0.5)
                if ft:
                    z.flip_done = True
                    fdir = -d
                    f_entry = z.bot if d > 0 else z.top
                    f_stop = z.top + 1 if d > 0 else z.bot - 1
                    tgt = self._opp_target(d, f_entry, self._k, fdir)
                    if tgt is None:
                        tgt = f_entry + fdir * abs(f_entry - f_stop) * 2
                    return self._enter("FLIP", fdir, f_entry, f_stop, tgt)
                if (b.c > z.top + 5) if d > 0 else (b.c < z.bot - 5):
                    z.flip_done = True   # ran away, no flip
        return []

    def _manage(self, b: Bar) -> list[Order]:
        t = self.trade
        assert t is not None
        d = t.dir
        t.bars_left -= 1

        def close(qty: int, tag: str, done: bool) -> list[Order]:
            qty = min(qty, t.remaining)
            if done:
                self.trade = None
            return [Order(self.symbol, -d, qty, tag=tag, reduce_only=True)] if qty > 0 else []

        if not t.scalped:
            scalp_hit = (b.h >= t.scalp_px) if d > 0 else (b.l <= t.scalp_px)
            stop_hit = (b.l <= t.stop) if d > 0 else (b.h >= t.stop)
            if scalp_hit:                       # scalp wins intrabar tie
                t.scalped = True
                t.stop = t.entry                # runner to breakeven
                scalp_qty = t.size // 2
                t.remaining -= scalp_qty
                return close(scalp_qty, "scale", done=(t.remaining <= 0)) if scalp_qty > 0 else []
            if stop_hit:
                return close(t.remaining, "stop", done=True)
        else:
            target_hit = (b.h >= t.target) if d > 0 else (b.l <= t.target)
            be_hit = (b.l <= t.stop) if d > 0 else (b.h >= t.stop)
            if target_hit:
                return close(t.remaining, "target", done=True)
            if be_hit:
                return close(t.remaining, "be", done=True)
        if t.bars_left <= 0:
            return close(t.remaining, "timeout", done=True)
        return []


__all__ = ["ZoneLifecycleStrategy"]
