"""PaintController — turns engine/strategy state into NT8 chart drawings.

What appears on the chart:
  - GHOST signals (light-blue arrows + [tag]): what the sleeves WOULD have done
    on the backfill days — the warmup-gate's suppressed orders, drawn at their
    historical time/price AS THE BACKFILL REPLAYS (immediate, not deferred), and
    capped per sleeve so a high-frequency sleeve can't flood the chart.
  - LIVE signals: bright solid arrows (lime up / red down) + "tag xN" at every
    real submitted order.
  - ZONES (zones sleeve): rectangles — VIRGIN demand green / supply red (vivid,
    the tradeable state), muted steel-blue once faded, and REMOVED once broken.
  - RISK line (open-drive): stop as a horizontal line while holding.
  - STATUS box (top-right): warmup/live, ghost/live counts, per-sleeve position.

All drawing is fire-and-forget and never touches the trading path.
"""
from __future__ import annotations

from .adapters.painter import NTChartPainter

NS = 1_000_000_000

GHOST = "#FFB0C4DE"          # light steel blue — clearly a "what-if", not a live fill
LIVE_UP = "#FF00E000"        # bright green
LIVE_DN = "#FFFF2020"        # bright red
ZONE_DEMAND = "#FF32CD32"    # solid lime; areaOpacity gives the fill transparency
ZONE_SUPPLY = "#FFFF4040"    # solid red
ZONE_FADED = "#FF4682B4"     # steel blue — touched (fade played) but not broken
OP_VIRGIN = 30               # NT areaOpacity (0-100) — vivid
OP_FADED = 12                # muted but readable

GHOST_CAP_PER_TAG = 80       # so ignition/flow can't bury open-drive/ibs/zones


class PaintController:
    def __init__(self, painter: NTChartPainter, strategies: list) -> None:
        self.p = painter
        self.strategies = strategies
        self._n_live = 0
        self._n_ghost = 0
        self._ghost_by_tag: dict[str, int] = {}
        self._zone_state: dict[str, object] = {}
        self._risk_on = False

    # ── signals ───────────────────────────────────────────────────────────
    async def ghost_one(self, ts: int, side: int, qty: int, tag: str, px: float) -> None:
        """One ghost arrow, painted live as the backfill replays. Per-tag capped."""
        base = tag.split("-")[0] if tag else "sig"
        if self._ghost_by_tag.get(base, 0) >= GHOST_CAP_PER_TAG:
            return
        self._ghost_by_tag[base] = self._ghost_by_tag.get(base, 0) + 1
        self._n_ghost += 1
        await self.p.arrow(f"eng-ghost-{self._n_ghost}", ts, px, side,
                           color=GHOST, label=f"[{tag}]")

    async def ghost_signals(self, signals: list) -> None:
        """Batch fallback: paint the most recent suppressed signals at once."""
        for ts, side, qty, tag, px in signals[-400:]:
            await self.ghost_one(ts, side, qty, tag, px)

    async def live_order(self, ts: int, side: int, qty: int, tag: str, px: float) -> None:
        self._n_live += 1
        await self.p.arrow(f"eng-live-{self._n_live}", ts, px, side,
                           color=LIVE_UP if side > 0 else LIVE_DN,
                           label=f"{tag} x{qty}")

    # ── per-1m-bar state repaint ──────────────────────────────────────────
    async def on_bar(self, ts: int, close: float, live: bool,
                     backfill_bars: int = 0) -> None:
        await self._paint_zones(ts)
        await self._paint_risk(ts)
        await self._paint_status(live, backfill_bars, close)

    async def _paint_zones(self, now_ts: int) -> None:
        for s in self.strategies:
            zones = getattr(s, "zones", None)
            if not isinstance(zones, list):
                continue
            for z in zones:
                if getattr(z, "ts", 0) == 0:
                    continue
                tag = f"eng-zone-{z.ts}"
                if z.broke:                              # dead: remove once, done
                    if self._zone_state.get(tag) != "gone":
                        self._zone_state[tag] = "gone"
                        await self.p.remove(tag)
                    continue
                if z.fade_done:
                    color, op = ZONE_FADED, OP_FADED
                else:
                    color, op = (ZONE_DEMAND if z.dir > 0 else ZONE_SUPPLY), OP_VIRGIN
                # redraw only when the visible state or the right edge (5m bucket)
                # changes — cheap enough to extend the box to "now" continuously
                state = (round(z.top, 2), round(z.bot, 2), z.fade_done,
                         now_ts // (5 * 60 * NS))
                if self._zone_state.get(tag) == state:
                    continue
                self._zone_state[tag] = state
                await self.p.rect(tag, z.ts, z.top, now_ts, z.bot, color=color, opacity=op)

    async def _paint_risk(self, ts: int) -> None:
        for s in self.strategies:
            if not hasattr(s, "stop") or not hasattr(s, "trail"):
                continue
            entered = getattr(s, "entered", False) and getattr(s, "pos", 0) != 0
            if entered:
                self._risk_on = True
                side = getattr(s, "side", 0)
                stop_px = s.entry_px - side * s.stop if s.stop < 100 else s.stop
                await self.p.hline("eng-od-stop", round(stop_px * 4) / 4, color="#FFFF4040")
            elif self._risk_on:
                self._risk_on = False
                await self.p.remove("eng-od-stop")

    async def _paint_status(self, live: bool, backfill_bars: int, close: float) -> None:
        head = "LIVE" if live else f"WARMUP {backfill_bars}b"
        lines = [f"ENGINE {head}  px {close:.2f}  ghosts {self._n_ghost} live {self._n_live}"]
        for s in self.strategies:
            name = type(s).__name__.replace("Strategy", "")
            if name == "Observe":
                continue
            pos = getattr(s, "pos", None)
            if pos is None:
                continue
            extra = ""
            if hasattr(s, "_F"):
                tgt = max(-s.maxp, min(s.maxp, s._F / s.scale))
                extra = f"  F={s._F:+.0f} tgt={tgt:+.1f}"
            if hasattr(s, "peak_fe") and pos:
                extra += f"  peak={s.peak_fe:+.1f}"
            flag = "  <== IN" if pos else ""
            lines.append(f"{name:<11} {pos:+d}{extra}{flag}")
        await self.p.status("\\n".join(lines))


__all__ = ["PaintController"]
