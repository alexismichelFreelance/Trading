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
from .core.timeutil import ns_to_utc
from .features.bars import BarAggregator
from .features.zones import DEMAND, ZoneBook, ZoneDetector

# the validated intraday session = 13:00-21:00 UTC (what claude_bars_1m uses).
# Intraday S/D zones are RTH-only per the methodology: overnight/Globex bars are
# low-volume and their levels don't carry the same weight. Live must match.
RTH_LO, RTH_HI = 13, 21

NS = 1_000_000_000

GHOST = "#FFB0C4DE"
LIVE_UP = "#FF00E000"
LIVE_DN = "#FFFF2020"
ZONE_DEMAND = "#FF32CD32"
ZONE_SUPPLY = "#FFFF4040"
RES_LINE = "#FFFF4040"       # resistance (supply above)
SUP_LINE = "#FF32CD32"       # support (demand below)
GEX_PUT = "#FF1E90FF"        # put wall  (gamma support)  — dodger blue
GEX_CALL = "#FFFFA500"       # call wall (gamma resistance) — orange
GEX_FLIP = "#FFBA90E0"       # zero-gamma flip (regime divider) — violet
PAPER = "#FF66CCCC"          # paper-sleeve fills (not routed to broker) — muted cyan
PAPER_CAP_PER_TAG = 12       # with 10 paper sleeves this bounds total chart objects

# timeframes shown, low->high. Higher TF = more opaque (more significant).
TF_ORDER = ("30m", "1h", "4h", "1d")
TF_OPACITY = {"30m": 16, "1h": 24, "4h": 34, "1d": 46}
TF_LABEL = {"4h": True, "1d": True}          # tag these on the chart

GHOST_CAP_PER_TAG = 6        # persistent chart objects — keep low so NT8 stays responsive


class ZoneView:
    """Independent MULTI-TIMEFRAME S/D zone lifecycle for VISUALIZATION.

    Intraday TFs (30m/1h/4h) are aggregated from the live 1m stream (exact,
    current contract). Daily is fed separately from daily bars (seed_daily),
    since a futures 'day' is a session, not a UTC calendar day, and needs long
    history for the detector's 20-bar window."""

    def __init__(self) -> None:
        self.agg = BarAggregator(("30m", "1h", "4h"))
        # gap-as-departure on intraday TFs; daily bars are already sessions
        self.det = {tf: ZoneDetector(gap_thr=5.0 if tf != "1d" else 0.0) for tf in TF_ORDER}
        self.book = {tf: ZoneBook() for tf in TF_ORDER}

    def _feed(self, tf: str, b) -> None:
        for z in self.det[tf].update(b):
            self.book[tf].add(z)
        self.book[tf].on_bar(b)
        self.book[tf].on_price(b.c, b.ts)

    def seed_daily(self, daily_bars: list) -> None:
        """Feed historical daily bars (built externally) to the 1d detector."""
        for b in daily_bars:
            self._feed("1d", b)

    def update(self, bar) -> None:
        if bar.tf == "1d":
            self._feed("1d", bar)
        elif RTH_LO <= ns_to_utc(bar.ts).hour < RTH_HI:   # RTH-only intraday zones
            for b in self.agg.update(bar):
                self._feed(b.tf, b)

    def active(self, tf: str) -> list:
        return [z for z in self.book[tf].zones if not z.broken]

    def all_active(self):
        for tf in TF_ORDER:
            for z in self.active(tf):
                yield tf, z

    def bracket(self, price: float):
        """Nearest unbroken supply above + demand below across ALL timeframes."""
        above = [(tf, z) for tf, z in self.all_active() if z.proximal() > price]
        below = [(tf, z) for tf, z in self.all_active() if z.proximal() < price]
        res = min(above, key=lambda p: p[1].proximal() - price, default=None)
        sup = max(below, key=lambda p: p[1].proximal(), default=None)
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
        self._n_paper = 0
        self._ghost_by_tag: dict[str, int] = {}
        self._paper_by_tag: dict[str, int] = {}
        self._zone_state: dict[str, object] = {}
        self._last_px = 0.0
        self._warm_n = 0
        self._gamma: dict | None = None       # prior-session gamma levels (ES terms)
        self._gamma_painted = False

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

    async def live_fill(self, f) -> None:
        """Paint at the ACTUAL fill ts+price (coincides with NT's native dot)."""
        self._n_live += 1
        side = 1 if f.size > 0 else -1
        await self.p.arrow(f"eng-fill-{self._n_live}", f.ts, f.price, side,
                           color=LIVE_UP if side > 0 else LIVE_DN,
                           label=f"{f.tag} @{f.price:.2f}")

    async def paper_fill(self, f) -> None:
        """Paint a PAPER (non-live) sleeve's fill in a distinct muted colour so
        every strategy's signals are visible without being confused for the few
        that route to the broker. Capped per tag to avoid clutter."""
        base = f.tag.split("-")[0] if f.tag else "paper"
        n = self._paper_by_tag.get(base, 0)
        if n >= PAPER_CAP_PER_TAG:
            return
        self._paper_by_tag[base] = n + 1
        self._n_paper += 1
        side = 1 if f.size > 0 else -1
        await self.p.arrow(f"eng-paper-{self._n_paper}", f.ts, f.price, side,
                           color=PAPER, label=f"~{f.tag}")

    # ── per-bar ───────────────────────────────────────────────────────────
    async def on_bar(self, bar, live: bool, backfill_bars: int = 0) -> None:
        self.zv.update(bar)                       # build zone history always
        self._last_px = bar.c
        if live:
            await self._paint_zones(bar.ts)
            await self._paint_bracket(bar.c)
            await self._paint_gamma(bar.ts)
            await self._paint_risk()
            await self._paint_status(True, backfill_bars, bar.c)
        else:
            self._warm_n += 1
            if self._warm_n % 500 == 0:
                await self._paint_status(False, backfill_bars, bar.c)

    async def _paint_zones(self, now_ts: int) -> None:
        bucket = now_ts // (10 * 60 * NS)          # extend zone rects every 10m, not 2m
        for tf in TF_ORDER:
            for z in self.zv.book[tf].zones:
                tag = f"eng-zone-{tf}-{z.formed_ts}-{z.direction}"
                if z.broken:
                    if self._zone_state.get(tag) != "gone":
                        self._zone_state[tag] = "gone"
                        await self.p.remove(tag)
                        await self.p.remove(tag + "-t")
                    continue
                color = ZONE_DEMAND if z.direction == DEMAND else ZONE_SUPPLY
                op = TF_OPACITY[tf] + (6 if z.virgin else 0)
                # daily rects are anchored to their own (session) time; intraday
                # to formed_ts; all extend to the live bar
                right = now_ts
                state = (round(z.top, 2), round(z.bot, 2), z.virgin, bucket)
                if self._zone_state.get(tag) == state:
                    continue
                self._zone_state[tag] = state
                await self.p.rect(tag, z.formed_ts, z.top, right, z.bot, color=color, opacity=op)
                if TF_LABEL.get(tf):
                    d = "D" if z.direction == DEMAND else "S"
                    await self.p.text(tag + "-t", z.formed_ts,
                                      z.top if z.direction < 0 else z.bot,
                                      f"{tf} {d}", color=color)

    async def _paint_bracket(self, price: float) -> None:
        res, sup = self.zv.bracket(price)
        if res is not None:
            await self.p.hline("eng-res", res[1].proximal(), color=RES_LINE)
        else:
            await self.p.remove("eng-res")
        if sup is not None:
            await self.p.hline("eng-sup", sup[1].proximal(), color=SUP_LINE)
        else:
            await self.p.remove("eng-sup")

    def set_gamma_levels(self, levels: dict | None) -> None:
        """Store prior-session gamma levels (ES terms) to draw as S/R lines."""
        self._gamma = levels
        self._gamma_painted = False

    async def _paint_gamma(self, now_ts: int) -> None:
        g = self._gamma
        if not g or self._gamma_painted:           # static prior-session levels:
            return                                  # draw once, not every bar
        self._gamma_painted = True
        reg = "long-gamma" if g["net_sign"] > 0 else "SHORT-gamma"
        rows = [("eng-gex-pw", g["put_wall"], GEX_PUT, "put wall"),
                ("eng-gex-cw", g["call_wall"], GEX_CALL, "call wall")]
        if g.get("flip") is not None:
            rows.append(("eng-gex-flip", g["flip"], GEX_FLIP, f"gamma flip ({reg})"))
        for tag, px, color, label in rows:
            await self.p.hline(tag, round(px * 4) / 4, color=color)
            await self.p.text(tag + "-t", now_ts, px, label, color=color)

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
        head = "LIVE" if live else f"WARMUP {backfill_bars}b"
        lines = [f"== ENGINE {head}  px {close:.2f} =="]
        r = f"R {res[1].proximal():.2f}({res[0]})" if res else "R --"
        s = f"S {sup[1].proximal():.2f}({sup[0]})" if sup else "S --"
        lines.append(f"{r}   {s}")
        counts = "  ".join(f"{tf}:{len(self.zv.active(tf))}" for tf in TF_ORDER)
        lines.append(f"zones  {counts}   sig {self._n_live}L/{self._n_ghost}G")
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
