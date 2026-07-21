"""LiveEngine — the async live/paper driver.

Concurrently consumes `feed.stream()` (market events) and `broker.events()`
(fills/positions), merging them into a single queue so the strategy state is
mutated by exactly one consumer at a time (no locks). Uses the WallClock. The
dispatch of events to strategies is the SAME `dispatch_market`/`dispatch_broker`
as ReplayEngine — the central invariant holds: identical Strategy objects,
identical routing; only feed/broker differ.

Reconnection is handled per-adapter: a pump task that hits an error backs off
and re-enters the adapter's stream (the adapter re-establishes its socket). A
cleanly-exhausted feed (e.g. a bounded test feed) drains pending broker events
and stops.
"""
from __future__ import annotations

import asyncio
import logging

from .blotter import Blotter
from .dispatch import dispatch_broker, dispatch_market
from .events import Bar, Fill, PositionUpdate, Trade
from .risk import RiskSupervisor

log = logging.getLogger("engine.live")

_END = ("end", None)


def _sign(x: int) -> int:
    return int(x > 0) - int(x < 0)


class LiveEngine:
    def __init__(self, feed, broker, strategies, clock, blotter: Blotter,
                 reconnect_delay: float = 1.0, drain_timeout: float = 0.5,
                 warmup_gate: bool = True, risk: RiskSupervisor | None = None,
                 live_owners: set | None = None) -> None:
        # Multi-instrument: `feed` may be one adapter or a list of them (one per
        # instrument lane); `broker` one adapter or {symbol: adapter}. The scalar
        # forms are exactly the pre-multi behavior.
        self.feeds = list(feed) if isinstance(feed, (list, tuple)) else [feed]
        self.feed = self.feeds[0]                    # back-compat alias
        if isinstance(broker, dict):
            self.brokers: dict[str, object] = dict(broker)
        else:
            self.brokers = {"": broker}              # "": catch-all (single mode)
        self.broker = next(iter(self.brokers.values()))   # back-compat alias
        self.strategies = list(strategies)
        self.clock = clock
        self.blotter = blotter
        self.reconnect_delay = reconnect_delay
        self.drain_timeout = drain_timeout
        # SAFETY: on connect the feed replays historical backfill bars (bars only,
        # no trades) so bar-driven features warm up. Those bars would otherwise
        # make strategies emit HISTORICAL orders that fill at the LIVE price. While
        # gated, we still dispatch events (state warms) but DROP the orders. The
        # first live Trade (backfill has none) flips us live. warmup_gate=False for
        # feeds with no backfill (tests).
        self.warmup_gate = warmup_gate
        # warmup is PER LANE (per event-symbol): the first live Trade of a lane
        # flips only that lane — a dead NQ feed can never keep ES in warmup.
        self._live_lanes: set[str] = set()
        self._bf_bars: dict[str, int] = {}
        self._suppressed_orders = 0
        # ghost signals: (ts, side, qty, tag, px, symbol) the strategies WOULD
        # have sent during warmup — consumed by the chart painter(s)
        self.warmup_signals: list[tuple[int, int, int, str, float, str]] = []
        # last trade/bar price per lane; "" holds the latest from any lane
        self._last_px: dict[str, float] = {"": 0.0}
        # optional async callbacks for the chart painter
        self.on_live_order = None          # async (ts, side, qty, tag, px) — decision time
        self.on_live_fill = None           # async (Fill) — ACTUAL engine fill ts+price
        self.on_manual_fill = None         # async (Fill) — unattributed (manual) fill
        self.on_bar_hook = None            # async (ts, close, live, backfill_bars)
        self.on_warmup_signal = None       # async (ts, side, qty, tag, px) — ghosts
        # ── per-strategy attribution ──────────────────────────────────────
        # Multiple sleeves share one ACCOUNT, so the account net position must
        # never be broadcast to strategies (each sleeve would mis-attribute the
        # others' inventory to itself — the 2026-07-07 runaway). Instead: every
        # order is owned by the strategy that emitted it; fills are routed ONLY
        # to their owner, which also receives a synthetic PositionUpdate of ITS
        # OWN book. Fills with unknown order_ids (manual trades) hit the blotter
        # only. reduce_only is enforced HERE against the owner's attributed
        # position (the NT path cannot enforce it).
        self._owner: dict[str, object] = {}         # order_id -> strategy
        self._spos: dict[int, int] = {}              # id(strategy) -> signed qty
        self._savg: dict[int, float] = {}            # id(strategy) -> avg px
        # paper positions carried across a restart (e.g. IBS held overnight),
        # rebuilt from claude_paper_fills by the runner. id(strategy)->(pos,avg);
        # applied at that lane's warmup->live flip so overnight sleeves resume
        # managing their position instead of orphaning it.
        self._restore: dict[int, tuple[int, float]] = {}
        # central risk supervisor (see core/risk.py). The default config has
        # every production limit OFF, but in-flight-aware reduce_only vetting
        # is always on — that closed the 2026-07-09 duplicate-flatten bug.
        self.risk = risk if risk is not None else RiskSupervisor()
        # PAPER vs LIVE routing. live_owners = set of id(strategy) that route to
        # the real broker (NT8); every other strategy ALWAYS paper-trades — its
        # orders fill inline at last_px, attributed to its own book, visible and
        # usable as signals, but never sent to the broker and never risk-
        # gated (we want to see the raw strategy). None = all strategies live
        # (backward compatible: tests + the pre-paper behavior).
        self.live_owners = live_owners
        self.on_paper_fill = None                   # async (Fill) for the painter
        self.paper_fills: list[Fill] = []
        self._q: asyncio.Queue = asyncio.Queue()
        self._stop = asyncio.Event()

    def _is_live(self, s) -> bool:
        return self.live_owners is None or id(s) in self.live_owners

    # ── back-compat views over the per-lane state ─────────────────────────
    @property
    def last_px(self) -> float:
        return self._last_px.get("", 0.0)            # latest from any lane

    def px_for(self, symbol: str) -> float:
        return self._last_px.get(symbol) or self._last_px.get("", 0.0)

    @property
    def _live(self) -> bool:                          # any lane live?
        return not self.warmup_gate or bool(self._live_lanes)

    @property
    def _backfill_bars(self) -> int:                  # total across lanes
        return sum(self._bf_bars.values())

    def _lane_live(self, sym: str) -> bool:
        return not self.warmup_gate or sym in self._live_lanes

    def _broker_for(self, symbol: str):
        if "" in self.brokers:                        # single-broker mode
            return self.brokers[""]
        b = self.brokers.get(symbol)
        if b is None:
            raise KeyError(f"no broker for symbol {symbol!r} "
                           f"(have {sorted(self.brokers)})")
        return b

    def restore_paper_position(self, strategy, pos: int, avg_px: float) -> None:
        """Register an open paper position to resume at the warmup->live flip
        (from claude_paper_fills). No-op for pos==0. Paper sleeves only — live
        positions live in the broker/account, not here."""
        if pos:
            self._restore[id(strategy)] = (pos, float(avg_px))

    def stop(self) -> None:
        self._stop.set()

    def strategy_position(self, s) -> int:
        return self._spos.get(id(s), 0)

    def _vet(self, s, o, ts):
        """All order safety is delegated to the RiskSupervisor: reduce_only
        against the owner's attributed book (in-flight aware), caps, rate
        limits, lockouts, halt."""
        return self.risk.vet(id(s), type(s).__name__, o, ts,
                             self._spos.get(id(s), 0))

    def _apply_restore(self, s) -> None:
        """At the live flip, hand a registered open position back to the sleeve.
        Only reseed the engine's attributed book if the sleeve confirms it can
        manage from (pos, avg) — otherwise the position would be held but never
        exited (worse than flat), so we drop it and log loudly."""
        r = self._restore.pop(id(s), None)
        if r is None or self._is_live(s):            # live positions aren't ours
            return
        pos, avg = r
        if getattr(s, "restore_state", None) and s.restore_state(pos, avg):
            self._spos[id(s)] = pos
            self._savg[id(s)] = avg
            log.info("restored paper position: %s %+d @ %.2f",
                     type(s).__name__, pos, avg)
        else:
            log.warning("open paper position for %s (%+d @ %.2f) NOT restorable "
                        "(sleeve needs richer state) — left flat, orphaned in "
                        "claude_paper_fills", type(s).__name__, pos, avg)

    async def _submit_owned(self, s, o) -> None:
        self._owner[o.order_id] = s
        self.blotter.on_order(o)
        await self._broker_for(o.symbol).submit(o)
        self.risk.on_submit(id(s), o)

    async def _paper_submit(self, s, o) -> None:
        """Fill a paper strategy's order inline at last_px (market model), against
        its OWN attributed book. reduce_only clamps/drops so the paper book stays
        coherent; no broker, no risk gate. Visible + recorded."""
        pid = id(s)
        pos = self._spos.get(pid, 0)
        qty = o.qty
        if o.reduce_only:
            if pos == 0 or _sign(o.side) == _sign(pos):
                return                               # nothing to reduce
            qty = min(qty, abs(pos))
        self._owner[o.order_id] = s
        f = Fill(self.clock.now(), o.order_id, o.symbol, self.px_for(o.symbol),
                 o.side * qty, 0.0, 0.0, o.tag)
        self.paper_fills.append(f)
        self._attribute_fill(f)                      # own book only (not live risk)
        if self.on_paper_fill is not None:
            await self.on_paper_fill(f)

    def _attribute_fill(self, f: Fill) -> None:
        s = self._owner.get(f.order_id)
        if s is None:
            log.info("unattributed fill (manual/external): %s", f)
            return
        pid = id(s)
        if self._is_live(s):                         # risk supervisor tracks LIVE only
            self.risk.on_fill(pid, f.order_id, f.size, f.price, f.symbol)
        old = self._spos.get(pid, 0)
        new = old + f.size
        if old == 0 or _sign(new) != _sign(old):
            self._savg[pid] = f.price               # fresh / flipped book
        elif _sign(f.size) == _sign(old):           # adding: weighted average
            self._savg[pid] = (self._savg[pid] * abs(old) + f.price * abs(f.size)) \
                / (abs(old) + abs(f.size))
        self._spos[pid] = new
        dispatch_broker([s], f)
        dispatch_broker([s], PositionUpdate(f.ts, f.symbol, new,
                                            self._savg.get(pid, f.price)))

    async def _pump_feed(self, feed) -> None:
        while not self._stop.is_set():
            try:
                async for e in feed.stream():
                    await self._q.put(("market", e))
                await self._q.put(_END)          # stream ended cleanly
                return
            except asyncio.CancelledError:
                raise
            except Exception as ex:              # noqa: BLE001 - resilient live loop
                log.warning("feed error: %s; reconnecting in %.1fs", ex, self.reconnect_delay)
                await asyncio.sleep(self.reconnect_delay)

    async def _pump_broker(self, broker) -> None:
        while not self._stop.is_set():
            try:
                async for be in broker.events():
                    await self._q.put(("broker", be))
                return
            except asyncio.CancelledError:
                raise
            except Exception as ex:              # noqa: BLE001
                log.warning("broker error: %s; reconnecting in %.1fs", ex, self.reconnect_delay)
                await asyncio.sleep(self.reconnect_delay)

    async def run(self) -> Blotter:
        feeders = [asyncio.create_task(self._pump_feed(f)) for f in self.feeds]
        # distinct broker objects only (a symbol->broker dict may share one)
        uniq_brokers = list({id(b): b for b in self.brokers.values()}.values())
        brokerers = [asyncio.create_task(self._pump_broker(b)) for b in uniq_brokers]
        feeds_done = 0
        try:
            while not self._stop.is_set():
                try:
                    kind, ev = await asyncio.wait_for(self._q.get(), timeout=self.drain_timeout)
                except asyncio.TimeoutError:
                    if feeds_done >= len(self.feeds):    # every feed ended -> done
                        break
                    continue
                if kind == "end":
                    feeds_done += 1
                    continue
                if kind == "market":
                    sym = getattr(ev, "symbol", "")
                    self.clock.set(ev.ts)
                    if isinstance(ev, Trade):
                        self._last_px[sym] = self._last_px[""] = ev.price
                        self.risk.note_price(sym, ev.price)   # $ caps always marked
                    elif isinstance(ev, Bar):
                        self._last_px[sym] = self._last_px[""] = ev.c
                        self.risk.note_price(sym, ev.c)
                    lane_live = self._lane_live(sym)
                    if not lane_live and isinstance(ev, Trade):
                        self._live_lanes.add(sym)        # this lane goes live
                        lane_live = True
                        for s in self.strategies:        # clear warmup phantom state
                            if getattr(s, "symbol", "") not in (sym, ""):
                                continue                 # other lanes keep warming up
                            reset = getattr(s, "reset_for_live", None)
                            if callable(reset):
                                reset()
                            self._apply_restore(s)       # resume overnight position
                        log.info("warmup complete [%s]: %d backfill bars consumed, %d "
                                 "warmup orders suppressed; lane strategies reset; now LIVE",
                                 sym or "-", self._bf_bars.get(sym, 0), self._suppressed_orders)
                    if not lane_live and isinstance(ev, Bar):
                        self._bf_bars[sym] = self._bf_bars.get(sym, 0) + 1
                    px = self.px_for(sym)
                    for s in self.strategies:            # per-strategy: orders are OWNED
                        for o in dispatch_market([s], ev):
                            if not lane_live:            # warmup: state only, no orders
                                self._suppressed_orders += 1
                                sig = (ev.ts, o.side, o.qty, o.tag, px, o.symbol)
                                self.warmup_signals.append(sig)
                                if self.on_warmup_signal is not None:
                                    await self.on_warmup_signal(*sig)   # ghost now
                            elif not self._is_live(s):   # PAPER: raw signal, inline fill
                                await self._paper_submit(s, o)
                            else:                        # LIVE: vetted -> real broker
                                o = self._vet(s, o, ev.ts)
                                if o is None:
                                    continue
                                await self._submit_owned(s, o)
                                if self.on_live_order is not None:
                                    await self.on_live_order(ev.ts, o.side, o.qty,
                                                             o.tag, self.px_for(o.symbol))
                    if self._live:                       # supervisor-owned actions (LIVE only)
                        books = [(id(s), type(s).__name__, s.symbol,
                                  self._spos.get(id(s), 0)) for s in self.strategies
                                 if self._is_live(s)]
                        for sid, o in self.risk.on_market(ev.ts, self._last_px, books):
                            owner = next(s for s in self.strategies if id(s) == sid)
                            await self._submit_owned(owner, o)
                    if isinstance(ev, Bar) and self.on_bar_hook is not None:
                        await self.on_bar_hook(ev, lane_live, self._bf_bars.get(sym, 0))
                    self.blotter.on_market_event(ev)
                else:  # broker event
                    self.blotter.on_broker_event(ev)
                    if isinstance(ev, Fill):
                        attributed = ev.order_id in self._owner
                        self._attribute_fill(ev)         # owner-only routing
                        # paint the ACTUAL fill (real ts+price). Engine fills ->
                        # on_live_fill. MANUAL fills do NOT show natively once the
                        # relay strategy is on the chart (NT hijacks the execution
                        # display), so we re-draw them ourselves via on_manual_fill.
                        if attributed and self.on_live_fill is not None:
                            await self.on_live_fill(ev)
                        elif not attributed and self.on_manual_fill is not None:
                            await self.on_manual_fill(ev)
                    # account-level PositionUpdates are NOT broadcast to
                    # strategies: each sleeve sees only its own attributed book
        finally:
            self._stop.set()
            for t in feeders + brokerers:
                t.cancel()
            await asyncio.gather(*feeders, *brokerers, return_exceptions=True)
        return self.blotter


__all__ = ["LiveEngine"]
