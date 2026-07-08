"""30-minute institutional supply/demand zones + lifecycle state machine.

Detector (per the validated research, SUPPLY_DEMAND_ZONES.md):
  departure bar j: range[j] >= 1.4*avg20(range), |c-o| >= 0.5*range[j],
                   vol[j] >= avg20(vol)        (a decisive, above-avg-vol move)
  base: 1-3 immediately-preceding bars with range <= 0.8*avg20(range)
  zone = [min(base lows), max(base highs)]; direction = sign(departure body)
    +1 demand (strong UP departure -> support below price)
    -1 supply (strong DOWN departure -> resistance above price)

Lifecycle: virgin (untested, highest probability) -> degrades each touch
(~dead after 2) -> a close fully through the zone BREAKS it and FLIPS polarity
(broken demand -> supply), single-use. Composite strength = departure(0-2) +
base(0-2); freshness (virgin) is the dominant fade gate.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from ..core.events import Bar
from ..core.timeutil import et_session_date

DEMAND = 1
SUPPLY = -1


@dataclass
class Zone:
    formed_ts: int
    top: float
    bot: float
    direction: int            # +1 demand (support), -1 supply (resistance)
    departure_score: int      # 0-2
    base_score: int           # 0-2
    is_gap: bool = False       # RTH-open gap zone (needs leave-and-return arming)
    touches: int = 0
    broken: bool = False
    broken_ts: int | None = None      # invalidation time (when a close went through)
    flipped_from: int | None = None   # the polarity it flipped from, if any
    last_touch_ts: int | None = None
    inside: bool = field(default=False, repr=False)

    @property
    def height(self) -> float:
        return self.top - self.bot

    @property
    def composite(self) -> int:
        return self.departure_score + self.base_score

    @property
    def virgin(self) -> bool:
        return self.touches == 0 and not self.broken

    def proximal(self) -> float:
        """Edge price reaches first on approach (demand: top; supply: bot)."""
        return self.top if self.direction > 0 else self.bot

    def distal(self) -> float:
        return self.bot if self.direction > 0 else self.top


class ZoneDetector:
    def __init__(self, avg_window: int = 20, dep_range_mult: float = 1.4,
                 dep_body_frac: float = 0.5, base_range_mult: float = 0.8,
                 max_base: int = 3, gap_thr: float = 0.0) -> None:
        self.win = avg_window
        self.dep_range_mult = dep_range_mult
        self.dep_body_frac = dep_body_frac
        self.base_range_mult = base_range_mult
        self.max_base = max_base
        # GAP-as-departure: the RTH-open gap is an imbalance (a strong departure)
        # the methodology treats as a fresh zone. gap_thr=0 disables. Validated
        # (strategy_lab/GAP_DEPARTURE_STUDY.md): ~doubles the opportunity set at
        # base-zone quality. Requires RTH-only bars (a gap only exists then).
        self.gap_thr = gap_thr
        self._bars: deque[Bar] = deque(maxlen=avg_window + max_base + 2)
        self._ranges: deque[float] = deque(maxlen=avg_window)
        self._vols: deque[float] = deque(maxlen=avg_window)
        self._last_sess: str | None = None
        self._last_close: float | None = None

    def update(self, bar: Bar) -> list[Zone]:
        """Feed a closed 30m bar; return any zones formed (gap and/or
        base->departure). A gap zone forms on the first bar of a new session."""
        zones: list[Zone] = []
        # GAP zone: first bar of a new RTH session, |open - prior close| >= thr
        if self.gap_thr > 0:
            sess = et_session_date(bar.ts)
            if self._last_sess is not None and sess != self._last_sess \
                    and self._last_close is not None:
                gap = bar.o - self._last_close
                if abs(gap) >= self.gap_thr:
                    d = DEMAND if gap > 0 else SUPPLY
                    top = max(self._last_close, bar.o)
                    bot = min(self._last_close, bar.o)
                    zones.append(Zone(bar.ts, top, bot, d, 2, 1, is_gap=True))
            self._last_sess = sess
            self._last_close = bar.c
        # base -> departure zone
        rng = bar.h - bar.l
        if len(self._ranges) >= self.win:
            avg_r = sum(self._ranges) / len(self._ranges)
            avg_v = sum(self._vols) / len(self._vols)
            is_dep = (rng >= self.dep_range_mult * avg_r
                      and abs(bar.c - bar.o) >= self.dep_body_frac * rng
                      and bar.v >= avg_v and rng > 0)
            if is_dep:
                base = []
                for b in reversed(self._bars):
                    if (b.h - b.l) <= self.base_range_mult * avg_r and len(base) < self.max_base:
                        base.append(b)
                    else:
                        break
                if base:
                    top = max(b.h for b in base)
                    bot = min(b.l for b in base)
                    direction = DEMAND if bar.c > bar.o else SUPPLY
                    dep_score = 1 + (1 if rng >= 2.2 * avg_r else 0)
                    base_r = max(b.h - b.l for b in base)
                    base_score = 2 if base_r <= 0.5 * avg_r else 1
                    zones.append(Zone(bar.ts, top, bot, direction, dep_score, base_score))
        self._bars.append(bar)
        self._ranges.append(rng)
        self._vols.append(bar.v)
        return zones


class ZoneBook:
    """Holds all detected zones and drives their lifecycle from price + bars."""

    def __init__(self) -> None:
        self.zones: list[Zone] = []

    def add(self, zone: Zone) -> None:
        self.zones.append(zone)

    def on_price(self, price: float, ts: int) -> None:
        """Count a touch each time price enters a (non-broken) zone band."""
        for z in self.zones:
            if z.broken:
                continue
            inside = z.bot <= price <= z.top
            if inside and not z.inside:
                z.touches += 1
                z.last_touch_ts = ts
            z.inside = inside

    def on_bar(self, bar: Bar) -> list[Zone]:
        """A 30m close fully through a zone BREAKS it and FLIPS polarity.
        Returns any newly-created flip zones."""
        new_flips = []
        for z in list(self.zones):
            if z.broken:
                continue
            broke = (z.direction > 0 and bar.c < z.bot) or (z.direction < 0 and bar.c > z.top)
            if broke:
                z.broken = True
                z.broken_ts = bar.ts
                flip = Zone(bar.ts, z.top, z.bot, -z.direction,
                            z.departure_score, z.base_score, flipped_from=z.direction)
                self.zones.append(flip)
                new_flips.append(flip)
        return new_flips

    def nearest_opposing(self, price: float, trade_dir: int, min_dist: float = 2.0,
                         virgin_only: bool = False) -> Zone | None:
        """Trend-mode target: long -> nearest opposing SUPPLY above; short ->
        nearest opposing DEMAND below. Per the reference the zone need only be
        ACTIVE (not broken) at entry; `virgin_only` restricts to untested."""
        cands = []
        for z in self.zones:
            if z.broken or (virgin_only and not z.virgin):
                continue
            if trade_dir > 0 and z.direction == SUPPLY and z.proximal() >= price + min_dist:
                cands.append(z)
            elif trade_dir < 0 and z.direction == DEMAND and z.proximal() <= price - min_dist:
                cands.append(z)
        if not cands:
            return None
        return min(cands, key=lambda z: abs(z.proximal() - price))


__all__ = ["Zone", "ZoneDetector", "ZoneBook", "DEMAND", "SUPPLY"]
