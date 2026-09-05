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
from .core.timeutil import et_minute_of_day, ns_to_utc
from .features.bars import BarAggregator
from .features.zones import DEMAND, ZoneBook, ZoneDetector

# the validated intraday session = 13:00-21:00 UTC (what claude_bars_1m uses).
# Intraday S/D zones are RTH-only per the methodology: overnight/Globex bars are
# low-volume and their levels don't carry the same weight. Live must match.
# ET MINUTES, not UTC hours. This was `RTH_LO, RTH_HI = 13, 21` compared against
# ns_to_utc(...).hour, i.e. 09:00-17:00 ET: half an hour of pre-open tape and a
# full hour of post-close fed the intraday zone detectors, and a zone drew every
# day at 15:00 on a UTC+2 chart. Being a fixed UTC hour it also slid by one at
# every DST change while the session did not. The rest of the codebase gates on
# et_minute_of_day; this was the last place that did not.
RTH_LO, RTH_HI = 9 * 60 + 30, 16 * 60

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
ARROW_KEEP = 80              # most-recent fill arrows re-asserted on top of zones

# timeframes shown, low->high. Higher TF = more opaque (more significant).
TF_ORDER = ("30m", "1h", "4h", "1d")
TF_OPACITY = {"30m": 16, "1h": 24, "4h": 34, "1d": 46}
TF_LABEL = {"4h": True, "1d": True}          # tag these on the chart

GHOST_CAP_PER_TAG = 6        # persistent chart objects — keep low so NT8 stays responsive
# HOW MANY ZONES TO DRAW per timeframe. The BOOK keeps every unbroken zone --
# that is the point of seeding, and bracket()/strategies read all of it. The
# CHART is a different question: seeding takes ES from ~8 unbroken zones to
# ~200, and 200 rectangles re-asserted every 4 minutes is how NT8 becomes
# unusable. Draw the ones nearest price, keep the rest in the model.
ZONE_DRAW_PER_TF = 6


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

    def seed_intraday(self, bars_1m: list) -> None:
        """Warm the 30m/1h/4h detectors from HISTORICAL 1-minute bars.

        WHY. ZoneDetector needs 20 bars of its own timeframe before it can score
        a departure (range >= 1.4 x avg20). RTH-only, that is 13 bars a session
        at 30m, 6.5 at 1h and 1.6 at 4h -- so fed from the live stream alone the
        1h detector holds ~6 bars and the 4h holds one or two, and NEITHER CAN
        EVER DETECT ANYTHING. A reversal level from last week could not become a
        zone, not because zones expire (ZoneBook never prunes) but because no
        detector had seen the bars. Only 1d had history, via seed_daily, which
        is why 1d was the only timeframe showing older structure.

        Deliberately routed through update(), the SAME path the live stream
        takes, so a seeded zone and a live one are produced by identical code
        and the RTH gate applies to both. Zones broken during the seeded history
        come up already broken, and the book is a pure function of the bars --
        so a restart REBUILDS it rather than losing it, and nothing has to be
        persisted.

        Lossy in one respect, stated because it matters: on_price counts touches
        tick by tick live, but a replay only sees bar closes, so seeded zones
        carry a coarser virgin/tested grade than live ones."""
        for b in bars_1m:
            self.update(b)

    def update(self, bar) -> None:
        if bar.tf == "1d":
            self._feed("1d", bar)
        elif RTH_LO <= et_minute_of_day(bar.ts) < RTH_HI:  # RTH-only intraday zones
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


def gamma_label(price: float, flip: float | None, net_sign: int) -> str:
    """The regime AT PRICE, for the chart.

    This was `"long-gamma" if net_sign > 0 else "SHORT-gamma"` -- net_sign being
    the sign of the WHOLE option book. Across the 44 rebuilt CBOE payloads that
    disagreed with the regime where price actually sat on 25 of 44 sessions
    (57%), and every disagreement was the same way round: book LONG, local
    SHORT. Spot sat below the flip continuously from 2026-08-06 to 08-13 on both
    SPX and NDX, so the chart said "pinning" through a fortnight that was
    structurally amplifying.

    Above the flip cumulative dealer gamma is positive (hedging damps moves);
    below it negative (hedging amplifies). With no flip in the book there is no
    boundary and the book sign is the only answer available -- 14 of the 44
    sessions were like that. With no price yet, say so rather than guess: a
    fallback to net_sign is precisely the bug being removed.
    """
    if not price:
        return "regime unknown"
    if flip is None:
        return "long-gamma" if net_sign > 0 else "SHORT-gamma"
    return "long-gamma" if price > flip else "SHORT-gamma"


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
        self._n_manual = 0
        self._ghost_by_tag: dict[str, int] = {}
        self._paper_by_tag: dict[str, int] = {}
        self._arrows: dict[str, tuple] = {}   # fill arrows to re-assert above zones
        self._arrow_bucket = -1
        self._last_px: float | None = None
        self._zone_state: dict[str, object] = {}
        self._last_px = 0.0
        self._warm_n = 0
        self._gamma: dict | None = None       # prior-session gamma levels (ES terms)
        self._gamma_bucket = -1

    # ── signals ───────────────────────────────────────────────────────────
    async def ghost_one(self, ts: int, side: int, qty: int, tag: str, px: float,
                        symbol: str = "") -> None:
        base = tag.split("-")[0] if tag else "sig"
        if self._ghost_by_tag.get(base, 0) >= GHOST_CAP_PER_TAG:
            return
        self._ghost_by_tag[base] = self._ghost_by_tag.get(base, 0) + 1
        self._n_ghost += 1
        await self.p.arrow(f"eng-ghost-{self._n_ghost}", ts, px, side,
                           color=GHOST, label=f"[{tag}]")

    async def ghost_signals(self, signals: list) -> None:
        for ts, side, qty, tag, px, *_ in signals[-200:]:
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
        tag = f"eng-fill-{self._n_live}"
        self._remember_arrow(tag, f.ts, f.price, side,
                             LIVE_UP if side > 0 else LIVE_DN, f"{f.tag} @{f.price:.2f}")
        await self.p.arrow(tag, f.ts, f.price, side,
                           color=LIVE_UP if side > 0 else LIVE_DN,
                           label=f"{f.tag} @{f.price:.2f}")

    async def manual_fill(self, f) -> None:
        """Re-draw a MANUAL (unattributed) fill. The relay strategy on the chart
        suppresses NT's native execution markers, so we redraw the user's own
        orders (green up / red down) to keep them visible."""
        self._n_manual += 1
        side = 1 if f.size > 0 else -1
        tag = f"eng-manual-{self._n_manual}"
        color = LIVE_UP if side > 0 else LIVE_DN
        label = f"{f.price:.2f}"
        self._remember_arrow(tag, f.ts, f.price, side, color, label)
        await self.p.arrow(tag, f.ts, f.price, side, color=color, label=label)

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
        tag = f"eng-paper-{self._n_paper}"
        self._remember_arrow(tag, f.ts, f.price, side, PAPER, f"~{f.tag}")
        await self.p.arrow(tag, f.ts, f.price, side, color=PAPER, label=f"~{f.tag}")

    def _remember_arrow(self, tag, ts, px, side, color, label) -> None:
        self._arrows[tag] = (ts, px, side, color, label)
        while len(self._arrows) > ARROW_KEEP:          # keep the most recent N
            self._arrows.pop(next(iter(self._arrows)))

    async def _reassert_arrows(self, now_ts: int) -> None:
        """Re-add fill arrows AFTER the zones each redraw cycle so they render on
        top (NT appends re-added objects) — otherwise the zone fills wash them out."""
        bucket = now_ts // (4 * 60 * NS)
        if bucket == self._arrow_bucket or not self._arrows:
            return
        self._arrow_bucket = bucket
        for tag, (ts, px, side, color, label) in list(self._arrows.items()):
            await self.p.remove(tag)                   # remove+re-add -> moves to front
            await self.p.arrow(tag, ts, px, side, color=color, label=label)

    # ── per-bar ───────────────────────────────────────────────────────────
    async def on_bar(self, bar, live: bool, backfill_bars: int = 0) -> None:
        self.zv.update(bar)                       # build zone history always
        self._last_px = bar.c
        if live:
            self._last_px = bar.c
            await self._paint_zones(bar.ts)
            await self._paint_bracket(bar.c)
            await self._paint_gamma(bar.ts)
            await self._paint_risk()
            await self._reassert_arrows(bar.ts)   # keep fill arrows above the zones
            await self._paint_status(True, backfill_bars, bar.c)
        else:
            self._warm_n += 1
            if self._warm_n % 500 == 0:
                await self._paint_status(False, backfill_bars, bar.c)

    async def _paint_zones(self, now_ts: int) -> None:
        bucket = now_ts // (4 * 60 * NS)           # re-assert zone rects every 4m
        # (fast enough to recover within minutes of a chart refresh, still ~2.5x
        # fewer redraws than the original 2m to keep NT8 responsive)
        for tf in TF_ORDER:
            # nearest-to-price subset, plus anything price is currently inside.
            # Everything else stays in the book and simply is not drawn; a zone
            # that scrolls out of the drawn set is removed from the chart by the
            # same "gone" path a broken one uses.
            live = [z for z in self.zv.book[tf].zones if not z.broken]
            if self._last_px is not None and len(live) > ZONE_DRAW_PER_TF:
                px = self._last_px
                live.sort(key=lambda z: 0.0 if z.bot <= px <= z.top
                          else min(abs(z.top - px), abs(z.bot - px)))
                keep = {id(z) for z in live[:ZONE_DRAW_PER_TF]}
            else:
                keep = {id(z) for z in live}
            for z in self.zv.book[tf].zones:
                tag = f"eng-zone-{tf}-{z.formed_ts}-{z.direction}"
                if z.broken or id(z) not in keep:
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
        self._gamma_bucket = -1

    async def _paint_gamma(self, now_ts: int) -> None:
        g = self._gamma
        bucket = now_ts // (4 * 60 * NS)           # re-assert every 4m so the lines
        if not g or bucket == self._gamma_bucket:  # recover after a chart refresh
            return                                  # (static levels; cheap, 3 objects)
        self._gamma_bucket = bucket
        reg = gamma_label(self._last_px, g.get("flip"), g["net_sign"])
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
        # ONLY WHAT IS IN THE MARKET. Listing all 40 sleeves made the panel a
        # wall of "+0" that had to be read to find the one line that mattered,
        # and it grew with every sleeve added. Flat sleeves are counted, not
        # named; an open position gets its entry and its open P&L, which is the
        # thing you actually want off a glance at the chart.
        held, flat = [], 0
        for s in self.strategies:
            name = type(s).__name__.replace("Strategy", "").replace("Following", "")
            if name == "Observe":
                continue
            pos = getattr(s, "pos", None)
            if pos is None:
                continue
            if not pos:
                flat += 1
                continue
            bits = f"{name:<10} {pos:+d}"
            ep = getattr(s, "entry_px", None)
            if ep:
                bits += f" @{ep:.2f} {(close - ep) * (1 if pos > 0 else -1):+.2f}"
            if hasattr(s, "_F"):
                tgt = max(-s.maxp, min(s.maxp, s._F / s.scale)) if s.scale else 0
                bits += f"  F={s._F:+.0f} tgt={tgt:+.1f}"
            if getattr(s, "_er", None) is not None:
                bits += f"  ER={s._er:.2f}"
            if hasattr(s, "peak_fe"):
                bits += f"  peak={s.peak_fe:+.1f}"
            held.append(bits)
        lines += held or ["flat"]
        lines.append(f"({flat} armed)" if held else f"({flat} sleeves armed)")
        await self.p.status("\\n".join(lines), pos=self.panel_pos)


__all__ = ["PaintController", "ZoneView"]
