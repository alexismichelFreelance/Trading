"""PaintController — turns engine/strategy state into NT8 chart drawings.

Priorities (what the user needs to SEE on one chart, at the current view):
  1. INFO PANEL (always visible, top-left): engine state, zone summary, and
     every sleeve's live position + key metric (flow F/target, ignition
     strength/ER, ibs, open-drive stop). Viewport-independent — this is the
     primary readout.
  2. ZONES: virgin demand/supply rectangles extending to the LIVE bar, vivid;
     removed once broken. Only painted when LIVE (during warmup "now" is a
     historical backfill time, which would draw them off-screen).
  3. LIVE signals: bright arrows at real orders (current time -> visible).
  4. GHOSTS: light-blue what-if arrows on the backfill days, hard-capped so they
     don't flood the chart (secondary; only visible if you scroll back).

All drawing is fire-and-forget and never touches the trading path.
"""
from __future__ import annotations

from .adapters.painter import NTChartPainter

NS = 1_000_000_000

GHOST = "#FFB0C4DE"
LIVE_UP = "#FF00E000"
LIVE_DN = "#FFFF2020"
ZONE_DEMAND = "#FF32CD32"
ZONE_SUPPLY = "#FFFF4040"
ZONE_FADED = "#FF4682B4"
OP_VIRGIN = 30
OP_FADED = 12

GHOST_CAP_PER_TAG = 15       # ghosts are secondary; keep the chart uncluttered


class PaintController:
    def __init__(self, painter: NTChartPainter, strategies: list) -> None:
        self.p = painter
        self.strategies = strategies
        self._n_live = 0
        self._n_ghost = 0
        self._ghost_by_tag: dict[str, int] = {}
        self._zone_state: dict[str, object] = {}
        self._risk_on = False
        self._live_now = 0            # latest LIVE bar ts (zones anchor to this)
        self._warm_status_every = 0

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

    # ── per-bar repaint ───────────────────────────────────────────────────
    async def on_bar(self, ts: int, close: float, live: bool,
                     backfill_bars: int = 0) -> None:
        if live:
            self._live_now = ts
            await self._paint_zones(ts)
            await self._paint_risk()
            await self._paint_status(True, backfill_bars, close)
        else:
            # warmup: don't paint zones (historical "now" -> off-screen); refresh
            # the panel only occasionally so it isn't 20k redraws
            self._warm_status_every += 1
            if self._warm_status_every % 500 == 0:
                await self._paint_status(False, backfill_bars, close)

    # ── zones (LIVE only, anchored to the live bar) ───────────────────────
    async def _paint_zones(self, now_ts: int) -> None:
        for s in self.strategies:
            zones = getattr(s, "zones", None)
            if not isinstance(zones, list):
                continue
            for z in zones:
                if getattr(z, "ts", 0) == 0:
                    continue
                tag = f"eng-zone-{z.ts}"
                if z.broke:
                    if self._zone_state.get(tag) != "gone":
                        self._zone_state[tag] = "gone"
                        await self.p.remove(tag)
                    continue
                if z.fade_done:
                    color, op = ZONE_FADED, OP_FADED
                else:
                    color, op = (ZONE_DEMAND if z.dir > 0 else ZONE_SUPPLY), OP_VIRGIN
                state = (round(z.top, 2), round(z.bot, 2), z.fade_done,
                         now_ts // (2 * 60 * NS))       # re-extend every 2 live min
                if self._zone_state.get(tag) == state:
                    continue
                self._zone_state[tag] = state
                await self.p.rect(tag, z.ts, z.top, now_ts, z.bot, color=color, opacity=op)

    async def _paint_risk(self) -> None:
        for s in self.strategies:
            if not hasattr(s, "stop") or not hasattr(s, "trail"):
                continue
            if getattr(s, "entered", False) and getattr(s, "pos", 0) != 0:
                self._risk_on = True
                side = getattr(s, "side", 0)
                stop_px = s.entry_px - side * s.stop if s.stop < 100 else s.stop
                await self.p.hline("eng-od-stop", round(stop_px * 4) / 4, color="#FFFF4040")
            elif self._risk_on:
                self._risk_on = False
                await self.p.remove("eng-od-stop")

    # ── the info panel (primary readout) ──────────────────────────────────
    def _zone_summary(self) -> str:
        tops = []
        for s in self.strategies:
            for z in getattr(s, "zones", []) or []:
                if getattr(z, "ts", 0) and not getattr(z, "broke", False) \
                        and not getattr(z, "fade_done", False):
                    tops.append((z.prox if hasattr(z, "prox") else z.top, z.dir))
        if not tops:
            return "zones: none active"
        d = sum(1 for _, dr in tops if dr > 0)
        u = len(tops) - d
        near = sorted(t for t, _ in tops)
        lvls = " ".join(f"{t:.0f}" for t in near[:6])
        return f"zones: {len(tops)} active ({d}D/{u}S)  {lvls}"

    async def _paint_status(self, live: bool, backfill_bars: int, close: float) -> None:
        head = "LIVE" if live else f"WARMUP {backfill_bars}b"
        lines = [f"== ENGINE {head}  px {close:.2f} =="]
        lines.append(self._zone_summary())
        lines.append(f"signals: {self._n_live} live / {self._n_ghost} ghost")
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
            if hasattr(s, "_er") and getattr(s, "_er", None) is not None:
                bits += f"  ER={s._er:.2f}"
            if hasattr(s, "peak_fe") and pos:
                bits += f"  peak={s.peak_fe:+.1f}"
            if pos:
                bits += "  <== IN"
            lines.append(bits)
        await self.p.status("\\n".join(lines))


__all__ = ["PaintController"]
