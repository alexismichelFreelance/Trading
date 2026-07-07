"""PaintController — turns engine/strategy state into NT8 chart drawings.

The chart shows, on one pane at the current view:
  1. INFO PANEL (bottom-left, always visible): engine state, S/R bracket, active
     zone count, and every sleeve's live position + key metric.
  2. ZONES: its OWN market-structure lifecycle (ZoneDetector + ZoneBook fed from
     the live bar stream, independent of any trading sleeve) — every UNBROKEN
     zone as a rectangle (virgin bright, tested dimmer), removed when broken.
  3. S/R BRACKET: the nearest unbroken SUPPLY above and DEMAND below price as
     labeled horizontal lines — always there while a zone exists on that side.
  4. LIVE signals: bright arrows at real orders. GHOSTS: capped what-if arrows.

Zones are computed on EVERY bar (so history warms up), painted only when LIVE
(during warmup "now" is a historical time -> off-screen). Fire-and-forget.
"""
from __future__ import annotations

from .adapters.painter import NTChartPainter
from .features.bars import BarAggregator
from .features.zones import DEMAND, ZoneBook, ZoneDetector

NS = 1_000_000_000

GHOST = "#FFB0C4DE"
LIVE_UP = "#FF00E000"
LIVE_DN = "#FFFF2020"
ZONE_DEMAND = "#FF32CD32"
ZONE_SUPPLY = "#FFFF4040"
OP_VIRGIN = 32
OP_TESTED = 14
RES_LINE = "#FFFF4040"       # resistance (supply above)
SUP_LINE = "#FF32CD32"       # support (demand below)

GHOST_CAP_PER_TAG = 15


class ZoneView:
    """Independent 30m S/D zone lifecycle for VISUALIZATION (not trading)."""

    def __init__(self) -> None:
        self.agg = BarAggregator(("30m",))
        self.det = ZoneDetector()
        self.book = ZoneBook()

    def update(self, bar) -> None:
        for b in self.agg.update(bar):
            z = self.det.update(b)
            if z is not None:
                self.book.add(z)
            self.book.on_bar(b)          # break/flip on close-through
            self.book.on_price(b.c, b.ts)

    def active(self) -> list:
        return [z for z in self.book.zones if not z.broken]

    def bracket(self, price: float):
        act = self.active()
        above = [z for z in act if z.proximal() > price]
        below = [z for z in act if z.proximal() < price]
        res = min(above, key=lambda z: z.proximal() - price, default=None)
        sup = max(below, key=lambda z: z.proximal(), default=None)
        return res, sup


class PaintController:
    def __init__(self, painter: NTChartPainter, strategies: list,
                 panel_pos: str = "bottomleft") -> None:
        self.p = painter
        self.strategies = strategies
        self.panel_pos = panel_pos
        self.zv = ZoneView()
        self._n_live = 0
        self._n_ghost = 0
        self._ghost_by_tag: dict[str, int] = {}
        self._zone_state: dict[str, object] = {}
        self._last_px = 0.0
        self._warm_n = 0

    # ── signals ───────────────────────────────────────────────────────────
    async def ghost_one(self, ts: int, side: int, qty: int, tag: str, px: float) -> None:
        base = tag.split("-")[0] if tag else "sig"
        if self._ghost_by_tag.get(base, 0) >= GHOST_CAP_PER_TAG:
            return
        self._ghost_by_tag[base] = self._ghost_by_tag.get(base, 0) + 1
        self._n_ghost += 1
        await self.p.arrow(f"eng-ghost-{self._n_ghost}", ts, px, side,
                           color=GHOST, label=f"[{tag}]")

    async def ghost_signals(self, signals: list) -> None:
        for ts, side, qty, tag, px in signals[-200:]:
            await self.ghost_one(ts, side, qty, tag, px)

    async def live_order(self, ts: int, side: int, qty: int, tag: str, px: float) -> None:
        self._n_live += 1
        await self.p.arrow(f"eng-live-{self._n_live}", ts, px, side,
                           color=LIVE_UP if side > 0 else LIVE_DN,
                           label=f"{tag} x{qty}")

    # ── per-bar ───────────────────────────────────────────────────────────
    async def on_bar(self, bar, live: bool, backfill_bars: int = 0) -> None:
        self.zv.update(bar)                       # build zone history always
        self._last_px = bar.c
        if live:
            await self._paint_zones(bar.ts)
            await self._paint_bracket(bar.c)
            await self._paint_risk()
            await self._paint_status(True, backfill_bars, bar.c)
        else:
            self._warm_n += 1
            if self._warm_n % 500 == 0:
                await self._paint_status(False, backfill_bars, bar.c)

    async def _paint_zones(self, now_ts: int) -> None:
        for z in self.zv.book.zones:
            tag = f"eng-zone-{z.formed_ts}-{z.direction}"
            if z.broken:
                if self._zone_state.get(tag) != "gone":
                    self._zone_state[tag] = "gone"
                    await self.p.remove(tag)
                continue
            color = ZONE_DEMAND if z.direction == DEMAND else ZONE_SUPPLY
            op = OP_VIRGIN if z.virgin else OP_TESTED
            state = (round(z.top, 2), round(z.bot, 2), z.virgin, now_ts // (2 * 60 * NS))
            if self._zone_state.get(tag) == state:
                continue
            self._zone_state[tag] = state
            await self.p.rect(tag, z.formed_ts, z.top, now_ts, z.bot, color=color, opacity=op)

    async def _paint_bracket(self, price: float) -> None:
        res, sup = self.zv.bracket(price)
        if res is not None:
            await self.p.hline("eng-res", res.proximal(), color=RES_LINE)
        else:
            await self.p.remove("eng-res")
        if sup is not None:
            await self.p.hline("eng-sup", sup.proximal(), color=SUP_LINE)
        else:
            await self.p.remove("eng-sup")

    async def _paint_risk(self) -> None:
        for s in self.strategies:
            if not (hasattr(s, "stop") and hasattr(s, "trail")):
                continue
            if getattr(s, "entered", False) and getattr(s, "pos", 0) != 0:
                side = getattr(s, "side", 0)
                stop_px = s.entry_px - side * s.stop if s.stop < 100 else s.stop
                await self.p.hline("eng-od-stop", round(stop_px * 4) / 4, color="#FFFFA500")
            else:
                await self.p.remove("eng-od-stop")

    async def _paint_status(self, live: bool, backfill_bars: int, close: float) -> None:
        res, sup = self.zv.bracket(close)
        act = self.zv.active()
        d = sum(1 for z in act if z.direction == DEMAND)
        head = "LIVE" if live else f"WARMUP {backfill_bars}b"
        lines = [f"== ENGINE {head}  px {close:.2f} =="]
        lines.append(f"R {res.proximal():.2f}" if res else "R  --")
        lines[-1] += f"   S {sup.proximal():.2f}" if sup else "   S  --"
        lines.append(f"zones: {len(act)} active ({d}D/{len(act)-d}S)  sig {self._n_live}L/{self._n_ghost}G")
        for s in self.strategies:
            name = type(s).__name__.replace("Strategy", "").replace("Following", "")
            if name == "Observe":
                continue
            pos = getattr(s, "pos", None)
            if pos is None:
                continue
            bits = f"{name:<10} {pos:+d}"
            if hasattr(s, "_F"):
                tgt = max(-s.maxp, min(s.maxp, s._F / s.scale)) if s.scale else 0
                bits += f"  F={s._F:+.0f} tgt={tgt:+.1f}"
            if getattr(s, "_er", None) is not None:
                bits += f"  ER={s._er:.2f}"
            if hasattr(s, "peak_fe") and pos:
                bits += f"  peak={s.peak_fe:+.1f}"
            if pos:
                bits += "  <== IN"
            lines.append(bits)
        await self.p.status("\\n".join(lines), pos=self.panel_pos)


__all__ = ["PaintController", "ZoneView"]
