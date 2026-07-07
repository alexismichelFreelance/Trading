"""PaintController — turns engine/strategy state into NT8 chart drawings.

What appears on the chart:
  - GHOST signals (hollow gray arrows + label): what the strategies WOULD have
    done on the backfill days — the warmup-gate's suppressed orders, drawn at
    their historical time and price.
  - LIVE signals: solid arrows (lime up / red down) with the order tag at every
    real submitted order.
  - ZONES (zones sleeve): rectangles colored by lifecycle state — virgin demand
    green / virgin supply red, dimmed once faded, gray when broken — extending
    to the current bar.
  - RISK lines (open-drive): stop and trail as horizontal lines while holding.
  - STATUS box (top-right): warmup/live state, flow F/target, positions.

Painting is throttled to 1m-bar boundaries for state (zones/status/lines);
arrows are event-driven. All drawing is fire-and-forget and never touches the
trading path.
"""
from __future__ import annotations

from .adapters.painter import NTChartPainter

GHOST = "#909399A6"          # translucent gray
LIVE_UP = "#FF32CD32"
LIVE_DN = "#FFFF4040"
ZONE_DEMAND = "#5532CD32"
ZONE_SUPPLY = "#55FF4040"
ZONE_FADED = "#33808080"


class PaintController:
    def __init__(self, painter: NTChartPainter, strategies: list) -> None:
        self.p = painter
        self.strategies = strategies
        self._n_arrow = 0
        self._zone_state: dict[str, tuple] = {}
        self._risk_on = False

    # ── signals ───────────────────────────────────────────────────────────
    async def ghost_signals(self, signals: list[tuple[int, int, int, str, float]]) -> None:
        """signals: (ts, side, qty, tag, px) captured during warmup."""
        for ts, side, qty, tag, px in signals[-400:]:      # object budget
            self._n_arrow += 1
            await self.p.arrow(f"eng-ghost-{self._n_arrow}", ts, px, side,
                               color=GHOST, label=f"[{tag}]")

    async def live_order(self, ts: int, side: int, qty: int, tag: str, px: float) -> None:
        self._n_arrow += 1
        await self.p.arrow(f"eng-live-{self._n_arrow}", ts, px, side,
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
            if zones is None or not isinstance(zones, list):
                continue
            for z in zones:
                if getattr(z, "ts", 0) == 0:
                    continue
                tag = f"eng-zone-{z.ts}"
                if z.broke:
                    color = ZONE_FADED
                elif z.fade_done:
                    color = ZONE_FADED
                else:
                    color = ZONE_DEMAND if z.dir > 0 else ZONE_SUPPLY
                state = (round(z.top, 2), round(z.bot, 2), z.fade_done, z.broke,
                         now_ts // (5 * 60 * 1_000_000_000))
                if self._zone_state.get(tag) == state:
                    continue                       # unchanged within 5m: skip redraw
                self._zone_state[tag] = state
                await self.p.rect(tag, z.ts, z.top, now_ts, z.bot, color=color)

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
        lines = [f"ENGINE {'LIVE' if live else f'WARMUP ({backfill_bars} bars)'}   px {close:.2f}"]
        for s in self.strategies:
            name = type(s).__name__.replace("Strategy", "")
            pos = getattr(s, "pos", None)
            if pos is None or name == "Observe":
                continue
            extra = ""
            if hasattr(s, "_F"):
                extra = f"  F={s._F:+.0f} tgt={max(-s.maxp, min(s.maxp, s._F / s.scale)):+.1f}"
            if hasattr(s, "peak_fe") and pos:
                extra += f"  peak_fe={s.peak_fe:+.1f}"
            lines.append(f"{name:<12} pos {pos:+d}{extra}")
        await self.p.status("\\n".join(lines))


__all__ = ["PaintController"]
