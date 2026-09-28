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
ARROW_KEEP = 80              # most-recent PAPER/MANUAL arrows re-asserted over zones

# LIVE TRADES ARE THE POINT OF THE CHART, so they get their own everything.
# Before this they shared one 80-deep arrow store with paper and manual fills:
# ten paper sleeves firing all day evicted the handful of marks that were
# actually routed to the broker, and a round trip was drawn as two unconnected
# arrows with no statement of what it made. A trade is now ONE line from entry
# to exit, coloured by outcome, labelled with the sleeve and the P&L.
TRADE_WIN = "#FF00FF7F"      # closed in profit — spring green
TRADE_LOSS = "#FFFF4500"     # closed at a loss — orange red
TRADE_OPEN = "#FFFFD700"     # STILL ON — gold, redrawn to the current bar
LIVE_KEEP = 400              # live arrows kept; ~a month of live trading
TRADE_KEEP = 200             # closed-trade lines + labels kept

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
                 panel_pos: str = "bottomleft", point_usd: float = 50.0) -> None:
        self.p = painter
        self.strategies = strategies
        self.panel_pos = panel_pos
        self.point_usd = point_usd
        # live trade bookkeeping, per SLEEVE label
        self._book: dict[str, tuple] = {}        # name -> (qty, avg_px, entry_ts)
        self._open_line: dict[str, tuple] = {}   # name -> (entry_ts, entry_px)
        self._live_arrows: dict[str, tuple] = {}  # never evicted by paper traffic
        self._trades: dict[str, tuple] = {}      # closed-trade lines
        self._trade_txt: dict[str, tuple] = {}   # their P&L labels
        self._n_trade = 0
        self._rail_state: dict[str, tuple] = {}  # rail -> (price, 4min bucket)
        # roster labels that route to the broker; the panel stars them
        self.live_labels: set = set()
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

    async def live_fill(self, f, sleeve: str = "") -> None:
        """Paint a ROUTED fill at its actual ts+price, and book it into a trade.

        Uncapped and kept in its own store: a live mark must never be evicted by
        paper traffic. The arrow says WHAT happened; _close_trade draws the line
        that says what the round trip was worth."""
        self._n_live += 1
        side = 1 if f.size > 0 else -1
        name = (sleeve or "live").split(":")[-1]
        tag = f"eng-fill-{self._n_live}"
        color = LIVE_UP if side > 0 else LIVE_DN
        label = f"{name} {f.tag} @{f.price:.2f}"
        self._live_arrows[tag] = (f.ts, f.price, side, color, label)
        while len(self._live_arrows) > LIVE_KEEP:
            self._live_arrows.pop(next(iter(self._live_arrows)))
        await self.p.arrow(tag, f.ts, f.price, side, color=color, label=label)
        await self._book_fill(name, f)

    # ── live trades: entry -> exit, as one object ────────────────────────
    async def _book_fill(self, name: str, f) -> None:
        """Average-cost per sleeve. A fill that returns the book to flat closes
        a trade and draws it."""
        qty, avg, ets = self._book.get(name, (0, 0.0, 0))
        q = int(f.size)
        if qty == 0 or (qty > 0) == (q > 0):                 # opening or adding
            navg = ((avg * abs(qty) + f.price * abs(q)) / (abs(qty) + abs(q))
                    if (abs(qty) + abs(q)) else f.price)
            self._book[name] = (qty + q, navg, ets or f.ts)
            if qty == 0:
                self._open_line[name] = (f.ts, f.price)
            return
        closed = min(abs(q), abs(qty))                       # reducing or closing
        d = 1 if qty > 0 else -1
        pts = (f.price - avg) * d
        left = qty + q
        if left == 0:
            self._book.pop(name, None)
            entry = self._open_line.pop(name, (ets, avg))
            await self._clear_open_marks(name)
            await self._close_trade(name, entry[0], avg, f.ts, f.price, pts, closed)
        else:                                                 # partial scale-out
            self._book[name] = (left, avg, ets)

    async def _close_trade(self, name, t0, p0, t1, p1, pts, qty) -> None:
        self._n_trade += 1
        col = TRADE_WIN if pts > 0 else TRADE_LOSS
        ln, tx = f"eng-trade-{self._n_trade}", f"eng-tradetxt-{self._n_trade}"
        usd = pts * self.point_usd * qty
        label = f"{name} {pts:+.2f}pt {usd:+,.0f}$"
        self._trades[ln] = (t0, p0, t1, p1, col)
        self._trade_txt[tx] = (t1, p1, label, col)
        while len(self._trades) > TRADE_KEEP:
            old = next(iter(self._trades))
            self._trades.pop(old)
            await self.p.remove(old)
        while len(self._trade_txt) > TRADE_KEEP:
            old = next(iter(self._trade_txt))
            self._trade_txt.pop(old)
            await self.p.remove(old)
        await self.p.line(ln, t0, p0, t1, p1, color=col, width=3, style="solid")
        await self.p.text(tx, t1, p1, label, color=col)

    def _sleeve(self, name: str):
        for s in self.strategies:
            lb = (getattr(s, "label", "") or "").split(":")[-1]
            if lb == name:
                return s
        return None

    async def paint_open_trades(self, bar) -> None:
        """Redraw everything about every OPEN live position: the in-progress leg
        from entry to now, and the levels the sleeve is working — ENTRY, TARGET
        and STOP as their own labelled rails.

        Every bar, not on the 4-minute re-assert bucket: these are the objects on
        the chart whose whole job is to be current. At most four per live sleeve,
        so the cost is a handful of messages a minute.

        Target and stop come from the sleeve's chart_marks(), which is a declared
        protocol — see BaseStrategy. A sleeve that does not implement it simply
        draws fewer rails; nothing is guessed."""
        for name, (t0, p0) in list(self._open_line.items()):
            tag = f"eng-open-{name}"
            await self.p.remove(tag)
            await self.p.line(tag, t0, p0, bar.ts, float(bar.c),
                              color=TRADE_OPEN, width=2, style="solid")
            s = self._sleeve(name)
            marks = s.chart_marks() if hasattr(s, "chart_marks") else {}
            # THE RAILS ARE NOT REDRAWN EVERY BAR. Entry, target and stop are
            # static for the life of the trade; only their right edge creeps
            # forward, which nobody can see arriving 4 minutes late. Five live
            # sleeves x three rails x (remove line, remove text, line, text) is
            # ~60 messages a minute for no visible gain, and redraw volume is
            # exactly how this chart was made unusable before. The CONNECTOR
            # above is the part that must be current, so it alone runs per bar.
            bucket = bar.ts // (4 * 60 * NS)
            for key, color in (("entry", TRADE_OPEN), ("target", TRADE_WIN),
                               ("stop", TRADE_LOSS)):
                px = marks.get(key)
                lt, tt = f"eng-{key}-{name}", f"eng-{key}txt-{name}"
                state = (round(float(px), 4) if px else None, bucket)
                if self._rail_state.get(lt) == state:
                    continue
                self._rail_state[lt] = state
                await self.p.remove(lt)
                await self.p.remove(tt)
                if not px:
                    continue
                await self.p.line(lt, t0, float(px), bar.ts, float(px),
                                  color=color, width=2,
                                  style="solid" if key == "entry" else "dash")
                await self.p.text(tt, bar.ts, float(px),
                                  f"{name} {key.upper()} {px:.2f}", color=color)

    async def _clear_open_marks(self, name: str) -> None:
        """Every rail belonging to a position that has just closed."""
        for t in (f"eng-open-{name}", f"eng-entry-{name}", f"eng-entrytxt-{name}",
                  f"eng-target-{name}", f"eng-targettxt-{name}",
                  f"eng-stop-{name}", f"eng-stoptxt-{name}"):
            await self.p.remove(t)
            self._rail_state.pop(t, None)   # else the next trade skips its redraw

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
        """Re-add fill marks AFTER the zones each redraw cycle so they render on
        top (NT appends re-added objects) — otherwise the zone fills wash them out.

        LIVE marks go last, so they end up above everything else on the chart
        including the paper ones. That ordering is the whole fix for "hidden
        behind": NT8 has no z-index, only append order."""
        bucket = now_ts // (4 * 60 * NS)
        if bucket == self._arrow_bucket:
            return
        if not (self._arrows or self._live_arrows or self._trades):
            return
        self._arrow_bucket = bucket
        for tag, (ts, px, side, color, label) in list(self._arrows.items()):
            await self.p.remove(tag)                   # remove+re-add -> moves to front
            await self.p.arrow(tag, ts, px, side, color=color, label=label)
        for tag, (t0, p0, t1, p1, col) in list(self._trades.items()):
            await self.p.remove(tag)
            await self.p.line(tag, t0, p0, t1, p1, color=col, width=3, style="solid")
        for tag, (ts, px, label, col) in list(self._trade_txt.items()):
            await self.p.remove(tag)
            await self.p.text(tag, ts, px, label, color=col)
        for tag, (ts, px, side, color, label) in list(self._live_arrows.items()):
            await self.p.remove(tag)
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
            await self._reassert_arrows(bar.ts)   # keep fill marks above the zones
            await self.paint_open_trades(bar)     # every bar: it must be current
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
            # The ROSTER LABEL when there is one, not the class name. Five zones
            # variants and eight trendjoin variants all share a class, so a panel
            # keyed on the class showed "ZoneLifecycle +1" with no way to tell
            # which of them was in the market. build_roster stamps .label.
            name = getattr(s, "label", "") or type(s).__name__
            name = name.split(":")[-1].replace("Strategy", "").replace("Following", "")
            if name in ("Observe", "ObserveOnly"):
                continue
            pos = getattr(s, "pos", None)
            if pos is None:
                continue
            if not pos:
                flat += 1
                continue
            # LIVE-routed sleeves are flagged, because "which of these is
            # actually in the market with the broker" is the first question the
            # panel has to answer now that some of them are.
            mark = "*" if name in self.live_labels else " "
            bits = f"{mark}{name:<16} {pos:+d}"
            ep = getattr(s, "entry_px", None)
            if ep:
                open_pts = (close - ep) * (1 if pos > 0 else -1)
                bits += (f" @{ep:.2f} {open_pts:+.2f}pt "
                         f"{open_pts * self.point_usd * abs(pos):+,.0f}$")
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
