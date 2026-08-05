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
from .dispatch import _wants, dispatch_broker, dispatch_market
from .events import Bar, Fill, PositionUpdate, Signal, Trade
from .orders import Order, OrderType
from .risk import RiskSupervisor
from .timeutil import et_session_date
from ..strategies.base import BaseStrategy

log = logging.getLogger("engine.live")

_END = ("end", None)


def _sign(x: int) -> int:
    return int(x > 0) - int(x < 0)


class LiveEngine:
    def __init__(self, feed, broker, strategies, clock, blotter: Blotter,
                 reconnect_delay: float = 1.0, drain_timeout: float = 0.5,
                 warmup_gate: bool = True, risk: RiskSupervisor | None = None,
                 live_owners: set | None = None,
                 stale_event_s: float = 60.0, sink_qsize: int = 4096) -> None:
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
        # ...and the EVENT TIMESTAMP that price came from. These two must travel
        # together. A paper fill used to be stamped self.clock.now(), which in
        # live mode is WallClock -- and WallClock.set() is a no-op, so the stamp
        # came from the OS while the price came from the event stream. Any lag
        # between market time and processing time therefore wrote a trade that
        # never happened: seconds-to-minutes of skew in normal running, HOURS
        # when NT8 replayed history into a running engine (2026-07-29: a fill
        # recorded at 16:53 ET priced 7459.50 while ES was 7337-7340, and
        # sleeves that hard-flatten at 15:59 logged fresh entries at 16:53).
        # claude_paper_fills is the table every sleeve is judged on, so this was
        # corrupting the only evidence we have.
        self._last_ts: dict[str, int] = {"": 0}
        # BACKFILL DETECTION, by event time alone. A live feed yields strictly
        # ascending timestamps (see FeedAdapter). A feed replaying history does
        # not: it jumps BACKWARDS. So an event more than `stale_event_s` behind
        # the newest event that lane has already delivered is history, whatever
        # the wall clock says. Deliberately not a wall-clock comparison -- that
        # would also flag replay, tests, and any synthetic timestamp, and would
        # need an opt-out at every call site that someone would eventually
        # forget. Regression against the lane's own high-water mark is the
        # anomaly itself. Startup backfill still ascends, so the warmup gate
        # keeps handling that as before; this catches backfill AFTER going live.
        self.stale_event_s = stale_event_s
        self._hwm: dict[str, int] = {}               # lane -> newest ts seen
        self._stale_events: dict[str, int] = {}
        # Paper orders that are not MARKET wait here until the tape trades through
        # their level (see _check_resting). [(strategy, Order)]
        self._resting: list[tuple] = []
        # LIVE orders the broker transport refused (socket dead/stalled). Counted
        # so a silent routing outage is visible instead of looking like "no signals".
        self.failed_orders = 0
        # id(strategy) -> session date already flattened (once per sleeve per day)
        self._session_flat: dict[int, str] = {}
        # ── SIDE-EFFECT SINKS ────────────────────────────────────────────────
        # The hooks below are OBSERVERS: chart painting and the QuestDB paper
        # blotter. They must never run inside the dispatch loop. They used to be
        # awaited there, which meant a stalled QuestDB insert or an NT8 draw
        # socket that stopped reading applied backpressure to TRADING -- and
        # because recording happens on the feed pump task, the engine kept
        # writing bars, ticks and per-second features and looked completely
        # healthy while placing no orders at all. tests/test_sink_backpressure.py
        # reproduces it: one stuck observer, and 1 of 6 events gets dispatched.
        # These now go on a bounded queue drained by its own task; the loop only
        # ever does put_nowait. On overflow work is DROPPED and counted, never
        # waited on -- losing a chart marker is acceptable, delaying an exit is
        # not.
        self._sink_q: asyncio.Queue | None = None
        self.sink_qsize = sink_qsize
        self._sink_dropped = 0
        self._sink_errors = 0
        # optional async callbacks for the chart painter
        self.on_live_order = None          # async (ts, side, qty, tag, px) — decision time
        self.on_live_fill = None           # async (Fill) — ACTUAL engine fill ts+price
        self.on_manual_fill = None         # async (Fill) — unattributed (manual) fill
        self.on_bar_hook = None            # async (ts, close, live, backfill_bars)
        self.on_warmup_signal = None       # async (ts, side, qty, tag, px) — ghosts
        # fires (once, live only) when the ET session date advances — lets the
        # runner refresh day-keyed snapshots (gamma regime + wall lines) so a
        # multi-day run stays correct without a restart.
        self.on_session_rollover = None    # async (new_day: str)
        self._session_day: str | None = None
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
        # orders fill inline at last_px, attributed to its own book, recorded
        # and painted, but never sent to the broker and never risk-gated (we
        # want to see the raw strategy). Peers see the INTENT via the Signal
        # channel, not via these fills. None = all strategies live
        # (backward compatible: tests + the pre-paper behavior).
        self.live_owners = live_owners
        self.on_paper_fill = None                   # async (Fill) for the painter
        self.paper_fills: list[Fill] = []
        # peer channel: every strategy's intent, broadcast to the others.
        # Advisory only -- carries NO position state, so it cannot reintroduce
        # the cross-sleeve duplicate-flatten bug that owner-only fill
        # attribution fixed. See dispatch_signal / BaseStrategy.on_signal.
        self.signals: list[Signal] = []
        self._peer_exits = 0                        # orders caused BY a peer
        # ── liveness: a lane going quiet must never be silent ──────────────
        self._lane_seen: dict[str, int] = {}         # lane -> ts of last event
        self._disconnects = 0
        self.on_feed_disconnect = None               # async (name, attempt, got)
        self.on_lane_stale = None                    # async (lane, seconds)
        self.stale_after_s = 120.0                   # warn if a lane goes quiet
        self._processed = 0                          # market events dispatched
        self._q: asyncio.Queue = asyncio.Queue()
        self._stop = asyncio.Event()

    async def wait_processed(self, n: int, timeout: float = 10.0,
                             poll: float = 0.005) -> int:
        """Block until the engine has DISPATCHED at least `n` market events.

        Tests drive a real feed adapter over a loopback socket, and a real feed
        is never `finite` -- a clean close is a disconnect now, so run() does not
        return on its own. The wrong way to end such a test is to sleep for a
        guessed duration: that is a race, it passes when nothing was processed,
        and it fails spuriously on a loaded machine. This waits on the engine's
        OWN progress instead, and raises if that progress never arrives.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._processed < n:
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"engine dispatched {self._processed}/{n} market events "
                    f"in {timeout}s")
            await asyncio.sleep(poll)
        return self._processed

    async def wait_idle(self, quiet: float = 0.15, timeout: float = 10.0,
                        poll: float = 0.005) -> int:
        """Block until the engine has processed events AND then gone quiet.

        Tests drive a real feed over a loopback socket. A real feed is never
        `finite` -- a clean close is a DISCONNECT now -- so run() never returns
        on its own and the test must decide when the tape is drained.

        This is NOT a sleep. It requires observed PROGRESS (at least one event
        dispatched) and then waits for that progress to stop changing, so a run
        that processes nothing raises instead of quietly "passing" -- which is
        exactly how a timing-based wait hides a dead engine. Counting expected
        events would be unreliable here because the feed also emits derived
        BookFlow/Bar events the caller cannot know about.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        seen, last_change = -1, loop.time()
        while True:
            await asyncio.sleep(poll)
            now = loop.time()
            if self._processed != seen:
                seen, last_change = self._processed, now
            elif seen > 0 and now - last_change >= quiet:
                # observers run off the hot path now, so "loop quiet" is not yet
                # "side effects landed". Tests assert on painted fills and
                # blotter rows, so drain that queue too -- otherwise this becomes
                # another wait that passes early.
                if self._sink_q is not None and (self._sink_q.qsize()
                                                 or self._sink_q._unfinished_tasks):
                    continue
                return seen
            if now >= deadline:
                raise TimeoutError(
                    f"engine never went idle: dispatched {self._processed} "
                    f"events in {timeout}s (0 means it processed NOTHING)")

    # ── side-effect sinks: never awaited by the dispatch loop ────────────────
    def _emit_sink(self, cb, *args) -> None:
        """Hand an observer callback to the drain task. Non-blocking by
        construction: no await, so trading can never queue behind it."""
        if cb is None:
            return
        if self._sink_q is None:                 # emitted outside run()
            self._sink_q = asyncio.Queue(self.sink_qsize)
        try:
            self._sink_q.put_nowait((cb, args))
        except asyncio.QueueFull:
            self._sink_dropped += 1
            if self._sink_dropped in (1, 100) or self._sink_dropped % 1000 == 0:
                log.error("SINK QUEUE FULL: dropped %d observer callbacks "
                          "(painting/blotter). Trading is UNAFFECTED by design, "
                          "but a sink is not keeping up -- check QuestDB and the "
                          "NT8 draw socket.", self._sink_dropped)

    async def _pump_sinks(self) -> None:
        """Drain observers off the hot path. One failing sink must not stop the
        others and must never reach the dispatch loop."""
        q = self._sink_q
        while True:
            cb, args = await q.get()
            try:
                await cb(*args)
            except asyncio.CancelledError:
                q.task_done()
                raise
            except Exception as ex:              # noqa: BLE001
                self._sink_errors += 1
                if self._sink_errors in (1, 10) or self._sink_errors % 100 == 0:
                    log.warning("sink callback failed (%d so far): %s: %s",
                                self._sink_errors, type(ex).__name__, ex)
            finally:
                q.task_done()

    def _is_live(self, s) -> bool:
        return self.live_owners is None or id(s) in self.live_owners

    # ── back-compat views over the per-lane state ─────────────────────────
    @property
    def last_px(self) -> float:
        return self._last_px.get("", 0.0)            # latest from any lane

    def px_for(self, symbol: str) -> float:
        return self._last_px.get(symbol) or self._last_px.get("", 0.0)

    def ts_for(self, symbol: str) -> int:
        """Timestamp of the event that produced px_for(symbol). Always use these
        two together — a price from one clock and a stamp from another is how the
        paper record got corrupted."""
        return self._last_ts.get(symbol) or self._last_ts.get("", 0)

    def _is_stale(self, sym: str, ts: int) -> bool:
        """True if this event regresses far behind the newest one this lane has
        already delivered — i.e. the feed is replaying history. Pure event time,
        so replay, tests and live all share one rule and there is nothing to
        configure per call site. Advances the lane's high-water mark."""
        hwm = self._hwm.get(sym)
        self._hwm[sym] = ts if hwm is None else max(hwm, ts)
        if hwm is None or self.stale_event_s <= 0:
            return False
        return (hwm - ts) > self.stale_event_s * 1e9

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

    def restore_paper_position(self, strategy, pos: int, avg_px: float,
                               close_px: float | None = None) -> None:
        """Register an open paper position to resume at the warmup->live flip
        (from claude_paper_fills). No-op for pos==0. Paper sleeves only — live
        positions live in the broker/account, not here.

        `close_px` is the price to flatten at IF the sleeve turns out not to be
        able to resume. The runner supplies it when the position's session has
        already ended, so an intraday sleeve is booked out at that session's
        close — where the engine's own 15:59 flat would have taken it — instead
        of wearing hours of overnight drift it was never exposed to by design.
        None means "close at the current mark", which is right for a restart
        inside the same session."""
        if pos:
            self._restore[id(strategy)] = (pos, float(avg_px),
                                           None if close_px is None else float(close_px))

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

    async def _apply_restore(self, s) -> None:
        """At the live flip, hand a registered open position back to the sleeve.

        Only reseed the engine's attributed book if the sleeve confirms it can
        manage from (pos, avg) — otherwise the position would be held but never
        exited, which is worse than flat.

        A sleeve that declines is right to: every intraday sleeve needs its entry
        ts, stop and MFE, none of which (pos, avg_px) can supply. But the position
        must still be CLOSED IN THE RECORD. It used to be merely dropped, leaving
        an entry with no exit in claude_paper_fills forever — 21 of them on
        2026-08-05, from the session that died at 11:10 the day before. Every
        number that sums fills (the scorecard, per-sleeve P&L, the promotion
        gate) then reads a position that never closed: the engine flat, the
        record long, the two never reconciling.

        So we book the flatten explicitly, through the normal paper path so it is
        attributed and observed like any other fill, tagged `restart-void` so no
        scorecard can mistake an accounting entry for a trading decision."""
        r = self._restore.pop(id(s), None)
        if r is None or self._is_live(s):            # live positions aren't ours
            return
        pos, avg, close_px = r
        if getattr(s, "restore_state", None) and s.restore_state(pos, avg):
            self._spos[id(s)] = pos
            self._savg[id(s)] = avg
            log.info("restored paper position: %s %+d @ %.2f",
                     type(s).__name__, pos, avg)
            return
        await self._close_orphan(s, pos, avg, close_px)

    async def _close_orphan(self, s, pos: int, avg: float,
                            close_px: float | None) -> None:
        """Close a position no sleeve will manage. HOW depends on whether it is
        still live.

        SAME SESSION (close_px is None -- the last fill is in the session still
        running). The position is real and open right now and the market is
        trading. An engine that resumes and cannot manage it is genuinely closing
        it, at the market, this instant. That is an ordinary fill with real P&L,
        tagged `restart-flat`. Voiding it would throw away a live trade: on
        2026-08-05 the engine was stopped at 11:00 ET and restarted at 12:25 ET,
        and every open position was voided as though it were a three-week-old
        orphan.

        FINISHED SESSION (the runner supplied that session's close). VOID it at
        its own entry for exactly zero P&L -- see below.

        The close price is deliberately NOT the market. An abandoned position is
        not evidence about the sleeve -- the engine was dead, the sleeve never got
        to manage its own exit, and whatever the market did afterwards it did
        without anyone watching. Marking these to any market price invents a
        result and writes it into claude_paper_fills, which is the table
        scorecard.py and promotion_check.py read to decide what earns real money.
        Neither of them reads the tag, so every invented point would count.

        Measured on the real record before this landed: 30 orphans going back to
        2026-07-17, worth +8159 points (~$180k) if marked to their session
        closes. That is not a P&L, it is a bug with a number attached.

        `close_px` is therefore used only to REPORT what was abandoned. The
        information belongs in the log, where a human weighs it, not in the
        table that gates capital."""
        sym = getattr(s, "symbol", "") or ""
        ts = self.ts_for(sym) or self.clock.now()
        mark = self.px_for(sym) or None
        live = close_px is None and mark is not None
        # Reseed the book so the reduce_only flatten has something to reduce,
        # then take the normal paper path: attribution, sink and blotter all see
        # a perfectly ordinary closing fill.
        self._spos[id(s)] = pos
        self._savg[id(s)] = avg
        px = float(mark) if live else float(avg)
        tag = "restart-flat" if live else "restart-void"
        await self._fill_paper(s, Order(sym, -_sign(pos), abs(pos), tag=tag,
                                        reduce_only=True), px, ts)
        if live:
            log.warning("open paper position for %s (%+d @ %.2f) NOT restorable "
                        "(sleeve needs richer state) — it is still LIVE this "
                        "session, so CLOSED at the market %.2f for %+.2f pts. "
                        "That is a real fill, not an accounting entry.",
                        type(s).__name__, pos, avg, px, (px - avg) * pos)
            return
        would = "" if (close_px or mark) is None else (
            f" It would have been {((close_px or mark) - avg) * pos:+.2f} pts at "
            f"{(close_px or mark):.2f}; that is NOT booked -- the engine was not "
            f"running to manage it.")
        log.warning("open paper position for %s (%+d @ %.2f) NOT restorable "
                    "(sleeve needs richer state) — its session has CLOSED, so "
                    "VOIDED at its entry price with zero invented P&L.%s",
                    type(s).__name__, pos, avg, would)

    async def _submit_owned(self, s, o) -> bool:
        """Send a LIVE order. Returns True if the broker accepted it.

        Order of operations matters and used to be wrong: the blotter was written
        BEFORE the send and risk.on_submit AFTER it, so a raise in between left
        the blotter holding an order that never existed while risk had no record
        of it -- two ledgers, silently diverged. Nothing caught the exception
        either, so it propagated out of run() and shut down every strategy in the
        process. A broker that cannot take an order is a bad order, not a dead
        engine: record ONLY what the broker accepted, log loudly, keep trading."""
        try:
            await self._broker_for(o.symbol).submit(o)
        except Exception as ex:                      # noqa: BLE001
            self.failed_orders += 1
            log.error("ORDER REJECTED BY TRANSPORT [%s %s %+d x%d %s]: %s: %s "
                      "-- not recorded; engine continues (%d failed so far)",
                      self.label_of(s), o.symbol, o.side, o.qty, o.tag,
                      type(ex).__name__, ex, self.failed_orders)
            return False
        self._owner[o.order_id] = s
        self.blotter.on_order(o)
        self.risk.on_submit(id(s), o)
        return True

    async def _emit_signal(self, emitter, o, ts: int, px: float) -> None:
        """Broadcast `emitter`'s intent to its peers and route any replies.

        Replies go through the same paper/live path as ordinary orders but are
        NOT re-broadcast -- one level deep only. That bound is what keeps a
        single order from cascading: A signals, B may exit, and it stops there.
        """
        sig = Signal(ts, o.symbol, self.label_of(emitter), o.side, o.qty,
                     o.tag, px, bool(getattr(o, "reduce_only", False)))
        self.signals.append(sig)
        for peer in self.strategies:
            if peer is emitter or not _wants(peer, o.symbol):
                continue
            fn = getattr(peer, "on_signal", None)   # structural protocol:
            if fn is None:                          # opting in is optional
                continue
            for reply in fn(sig) or []:
                self._peer_exits += 1
                if not self._is_live(peer):
                    await self._paper_submit(peer, reply)
                else:
                    r = self._vet(peer, reply, ts)
                    if r is not None:
                        await self._submit_owned(peer, r)

    def label_of(self, s) -> str:
        """Roster label of a strategy ('ES:onbreak'), so peers can react to a
        NAMED source rather than to anything that moves. Falls back to the
        class name when the runner did not set one."""
        return getattr(s, "label", None) or type(s).__name__

    async def _paper_submit(self, s, o) -> None:
        """Route a paper order: MARKET fills now, STOP/LIMIT RESTS until the market
        trades through its level.

        Resting is the whole point. Every order used to fill at px_for(symbol) --
        the price *now* -- so a sleeve could only implement a stop by watching bar
        closes and then market-selling. On 2026-07-31 ES:zones_gap did exactly
        that: sized for a $2,000 loss on a ~7.5pt stop, it noticed the breach at a
        1-minute bar close and sold 25 points below the level, losing $12,512 --
        6x the risk its own sizing was computed from. With the level held here, a
        stop can only be beaten by a genuine gap, which is real market risk rather
        than the engine not looking."""
        if o.type in (OrderType.STOP, OrderType.LIMIT):
            self._resting.append((s, o))
            return
        await self._fill_paper(s, o, self.px_for(o.symbol),
                               self.ts_for(o.symbol) or self.clock.now())

    async def _flatten_for_session(self, sym: str, ts: int) -> None:
        """Flatten every intraday sleeve on this lane once the session is over.

        Fires at most once per sleeve per session date, so it cannot fight a
        sleeve that is already exiting, and never touches one that declares
        `holds_overnight = True` (IBS)."""
        if not BaseStrategy.session_over(ts):
            return
        day = et_session_date(ts)
        for s in self.strategies:
            if getattr(s, "symbol", "") not in (sym, ""):
                continue
            if getattr(s, "holds_overnight", False):
                continue
            pid = id(s)
            if self._session_flat.get(pid) == day:
                continue
            pos = self._spos.get(pid, 0)
            if pos == 0:
                continue
            self._session_flat[pid] = day
            side = -1 if pos > 0 else 1
            log.info("session flat [%s]: %s still %+d at the close",
                     sym or "-", self.label_of(s), pos)
            o = Order(s.symbol or sym, side, abs(pos), tag="session-flat",
                      reduce_only=True)
            if self._is_live(s):
                await self._submit_owned(s, o)
            else:
                await self._paper_submit(s, o)

    async def _check_resting(self, sym: str, px: float, ts: int) -> None:
        """Fill any resting order this price trades through. Called on EVERY market
        event, which is what keeps a stop honest: the trigger price is the first
        price that actually traded at or beyond the level, so the only way to be
        filled far from it is a real gap."""
        if not self._resting:
            return
        still: list[tuple] = []
        for s, o in self._resting:
            if o.symbol not in (sym, ""):
                still.append((s, o))
                continue
            if o.type is OrderType.STOP:
                hit = px <= o.stop_price if o.side < 0 else px >= o.stop_price
            else:                                    # LIMIT: fill at or better
                hit = px <= o.limit_price if o.side > 0 else px >= o.limit_price
            if not hit:
                still.append((s, o))
                continue
            await self._fill_paper(s, o, px, ts)     # the price that triggered it
        self._resting = still

    async def _fill_paper(self, s, o, price: float, ts: int) -> None:
        pid = id(s)
        pos = self._spos.get(pid, 0)
        qty = o.qty
        if o.reduce_only:
            if pos == 0 or _sign(o.side) == _sign(pos):
                return                               # nothing to reduce
            qty = min(qty, abs(pos))
        self._owner[o.order_id] = s
        # Stamp with the EVENT that priced this fill, never the wall clock: the
        # two must not come from different clocks (see _last_ts). Falls back to
        # clock.now() only before any market event has arrived.
        f = Fill(ts, o.order_id, o.symbol, price, o.side * qty, 0.0, 0.0, o.tag)
        self.paper_fills.append(f)
        self._attribute_fill(f)                      # own book only (not live risk)
        if self.on_paper_fill is not None:
            self._emit_sink(self.on_paper_fill, f)

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
        """Pump one feed forever, reconnecting on ANY end of stream.

        A live socket usually dies CLEANLY, not with an exception: NT8's relay
        stops (chart closed, strategy reset by the DOM close button, connection
        dropped) and asyncio's StreamReader simply ends its iteration. The old
        code treated that as "the feed is finished", emitted _END and RETURNED,
        so the reconnect path below was unreachable for the most common failure.
        On 2026-07-28 the ES lane died at 11:47:40 ET that way and never came
        back: every ES stream (ticks/depth/sec/bars) stopped in the same second
        while NQ kept running, so the engine looked healthy and silently traded
        one instrument for the rest of the session. 4h15m of ES capture lost.

        A clean end is now a DISCONNECT and is retried, loudly. Only a feed that
        declares `finite = True` (bounded test feeds) is allowed to end the
        stream for good -- termination is something the adapter states, never
        something inferred from how the iteration happened to stop.
        """
        name = getattr(feed, "symbol", "") or type(feed).__name__
        finite = bool(getattr(feed, "finite", False))
        attempt = 0
        while not self._stop.is_set():
            try:
                got = 0
                async for e in feed.stream():
                    got += 1
                    self._lane_seen[getattr(e, "symbol", "") or name] = self.clock.now()
                    await self._q.put(("market", e))
                if finite:
                    await self._q.put(_END)      # bounded feed: genuinely done
                    return
                attempt += 1
                self._disconnects += 1
                log.warning("FEED %s ended cleanly after %d events (disconnect "
                            "#%d) -- reconnecting in %.1fs", name, got, attempt,
                            self.reconnect_delay)
                if self.on_feed_disconnect is not None:
                    await self.on_feed_disconnect(name, attempt, got)
                await asyncio.sleep(self.reconnect_delay)
            except asyncio.CancelledError:
                raise
            except Exception as ex:              # noqa: BLE001 - resilient live loop
                attempt += 1
                self._disconnects += 1
                log.warning("FEED %s error: %s (disconnect #%d); reconnecting "
                            "in %.1fs", name, ex, attempt, self.reconnect_delay)
                if self.on_feed_disconnect is not None:
                    await self.on_feed_disconnect(name, attempt, -1)
                await asyncio.sleep(self.reconnect_delay)

    async def _watchdog(self) -> None:
        """A lane that has gone quiet must be LOUD about it.

        Reconnecting is not enough on its own: if the relay stays down, the pump
        retries forever and the engine still looks healthy. This is the second
        line -- it reports any lane that has produced no events for
        `stale_after_s`, and keeps reporting until it comes back. Silence is the
        one thing a live system must never do.
        """
        warned: set[str] = set()
        while not self._stop.is_set():
            await asyncio.sleep(min(self.stale_after_s / 4.0, 15.0))
            now = self.clock.now()
            for lane, seen in list(self._lane_seen.items()):
                quiet = (now - seen) / 1e9
                if quiet >= self.stale_after_s:
                    log.warning("LANE %s SILENT for %.0fs (last event %.0fs ago)",
                                lane, quiet, quiet)
                    warned.add(lane)
                    if self.on_lane_stale is not None:
                        await self.on_lane_stale(lane, quiet)
                elif lane in warned:
                    log.warning("LANE %s recovered after silence", lane)
                    warned.discard(lane)

    async def _pump_broker(self, broker) -> None:
        """Same contract as _pump_feed: a clean end is a DISCONNECT, not the end
        of the world. A broker socket dying quietly would otherwise stop order
        routing while the engine happily kept trading against a dead pipe."""
        name = type(broker).__name__
        finite = bool(getattr(broker, "finite", False))
        while not self._stop.is_set():
            try:
                async for be in broker.events():
                    await self._q.put(("broker", be))
                if finite:
                    return
                self._disconnects += 1
                log.warning("BROKER %s ended cleanly -- reconnecting in %.1fs",
                            name, self.reconnect_delay)
                await asyncio.sleep(self.reconnect_delay)
            except asyncio.CancelledError:
                raise
            except Exception as ex:              # noqa: BLE001
                self._disconnects += 1
                log.warning("BROKER %s error: %s; reconnecting in %.1fs",
                            name, ex, self.reconnect_delay)
                await asyncio.sleep(self.reconnect_delay)

    async def run(self) -> Blotter:
        # observers drain on their own task: nothing they touch (QuestDB, the
        # NT8 draw socket) can apply backpressure to trading
        self._sink_q = asyncio.Queue(self.sink_qsize)
        sinker = asyncio.create_task(self._pump_sinks())
        feeders = [asyncio.create_task(self._pump_feed(f)) for f in self.feeds]
        # liveness watchdog: only meaningful for an unbounded (live) feed set
        dog = (asyncio.create_task(self._watchdog())
               if any(not getattr(f, "finite", False) for f in self.feeds) else None)
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
                    self._processed += 1          # progress, for wait_processed
                    sym = getattr(ev, "symbol", "")
                    self.clock.set(ev.ts)
                    if isinstance(ev, Trade):
                        self._last_px[sym] = self._last_px[""] = ev.price
                        self._last_ts[sym] = self._last_ts[""] = ev.ts
                        self.risk.note_price(sym, ev.price)   # $ caps always marked
                    elif isinstance(ev, Bar):
                        self._last_px[sym] = self._last_px[""] = ev.c
                        self._last_ts[sym] = self._last_ts[""] = ev.ts
                        self.risk.note_price(sym, ev.c)
                    lane_live = self._lane_live(sym)
                    # STALE EVENT GUARD. The warmup gate only covers STARTUP: once
                    # a lane has gone live it stays live, so a mid-session backfill
                    # (NT8 reloading a chart, a data-session boundary, a reconnect)
                    # was replayed into live strategies and filled as real trades.
                    # An event far behind the wall clock is history by definition.
                    # State still warms; orders are dropped, exactly as in warmup.
                    # A stale Trade must ALSO not flip a lane live -- it is not
                    # evidence that live data has started, it is evidence of replay.
                    stale = self._is_stale(sym, ev.ts)
                    if stale:
                        n = self._stale_events[sym] = self._stale_events.get(sym, 0) + 1
                        if n == 1 or n % 500 == 0:
                            log.error("BACKFILL INTO A LIVE LANE [%s]: %d events, "
                                      "latest %s is %.0fs BEHIND the newest already "
                                      "seen. Warming state but placing NOTHING. The "
                                      "feed is replaying history (NT8 chart reload / "
                                      "data-session boundary).",
                                      sym or "-", n, type(ev).__name__,
                                      (self._hwm.get(sym, ev.ts) - ev.ts) / 1e9)
                    if not lane_live and not stale and isinstance(ev, Trade):
                        self._live_lanes.add(sym)        # this lane goes live
                        lane_live = True
                        for s in self.strategies:        # clear warmup phantom state
                            if getattr(s, "symbol", "") not in (sym, ""):
                                continue                 # other lanes keep warming up
                            reset = getattr(s, "reset_for_live", None)
                            if callable(reset):
                                reset()
                            await self._apply_restore(s)  # resume, or book it out
                        log.info("warmup complete [%s]: %d backfill bars consumed, %d "
                                 "warmup orders suppressed; lane strategies reset; now LIVE",
                                 sym or "-", self._bf_bars.get(sym, 0), self._suppressed_orders)
                    if not lane_live and isinstance(ev, Bar):
                        self._bf_bars[sym] = self._bf_bars.get(sym, 0) + 1
                    if stale:
                        lane_live = False        # history: warm state, place nothing
                    # ET session rollover (live only, once per change): refresh
                    # day-keyed snapshots. Warmup backfill (pre-live) is skipped
                    # so replayed historical days don't fire it.
                    if self._live and self.on_session_rollover is not None:
                        d = et_session_date(ev.ts)
                        if self._session_day is None:
                            self._session_day = d
                        elif d != self._session_day:
                            self._session_day = d
                            self._emit_sink(self.on_session_rollover, d)
                    px = self.px_for(sym)
                    # SESSION BOUNDARY, enforced centrally. Sleeves declare
                    # holds_overnight; a declaration nothing acts on is only a
                    # comment, and per-sleeve discipline is exactly what failed
                    # (zones_strategy and ignition both shipped with no end-of-day
                    # flat at all). Doing it here means a sleeve written tomorrow
                    # inherits it, and forgetting costs a flat position rather
                    # than an unplanned overnight one.
                    if lane_live:
                        await self._flatten_for_session(sym, ev.ts)
                    # Resting stops/limits are checked against EVERY price, before
                    # strategies see the event. That ordering matters: a protective
                    # stop must be beaten by the market, never by a sleeve reacting
                    # to the same tick first.
                    if lane_live and self._resting:
                        await self._check_resting(sym, px, ev.ts)
                    for s in self.strategies:            # per-strategy: orders are OWNED
                        for o in dispatch_market([s], ev):
                            # PEER CHANNEL: broadcast this intent to the other
                            # sleeves BEFORE routing it, so a peer can react in
                            # the same tick. Advisory only -- no position state
                            # crosses, so this cannot reintroduce the
                            # 2026-07-09 cross-sleeve duplicate-flatten bug.
                            # Peer replies are routed through the SAME paper/
                            # live path below, one level deep only: a reply
                            # never re-broadcasts, so one order can never
                            # cascade into a feedback loop.
                            if lane_live:
                                await self._emit_signal(s, o, ev.ts, px)
                            if not lane_live:            # warmup: state only, no orders
                                self._suppressed_orders += 1
                                sig = (ev.ts, o.side, o.qty, o.tag, px, o.symbol)
                                self.warmup_signals.append(sig)
                                if self.on_warmup_signal is not None:
                                    self._emit_sink(self.on_warmup_signal, *sig)
                            elif not self._is_live(s):   # PAPER: raw signal, inline fill
                                await self._paper_submit(s, o)
                            else:                        # LIVE: vetted -> real broker
                                o = self._vet(s, o, ev.ts)
                                if o is None:
                                    continue
                                if not await self._submit_owned(s, o):
                                    continue          # transport refused it
                                if self.on_live_order is not None:
                                    self._emit_sink(self.on_live_order, ev.ts,
                                                    o.side, o.qty, o.tag,
                                                    self.px_for(o.symbol))
                    if self._live:                       # supervisor-owned actions (LIVE only)
                        books = [(id(s), type(s).__name__, s.symbol,
                                  self._spos.get(id(s), 0)) for s in self.strategies
                                 if self._is_live(s)]
                        for sid, o in self.risk.on_market(ev.ts, self._last_px, books):
                            owner = next(s for s in self.strategies if id(s) == sid)
                            await self._submit_owned(owner, o)
                    if isinstance(ev, Bar) and self.on_bar_hook is not None:
                        self._emit_sink(self.on_bar_hook, ev, lane_live,
                                        self._bf_bars.get(sym, 0))
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
                            self._emit_sink(self.on_live_fill, ev)
                        elif not attributed and self.on_manual_fill is not None:
                            self._emit_sink(self.on_manual_fill, ev)
                    # account-level PositionUpdates are NOT broadcast to
                    # strategies: each sleeve sees only its own attributed book
        finally:
            self._stop.set()
            for t in feeders + brokerers:
                t.cancel()
            await asyncio.gather(*feeders, *brokerers, return_exceptions=True)
            if dog is not None:
                dog.cancel()
            # bounded chance for observers to land (so shutdown does not lose the
            # last blotter rows), then stop regardless -- shutdown must not hang
            # on a stuck sink either.
            try:
                await asyncio.wait_for(self._sink_q.join(),
                                       timeout=self.drain_timeout * 4)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                log.warning("sink queue still had %d items at shutdown",
                            self._sink_q.qsize())
            sinker.cancel()
            await asyncio.gather(sinker, return_exceptions=True)
            self._report_unapplied_restores()
        return self.blotter

    def _report_unapplied_restores(self) -> None:
        """Restores are applied at the warmup->live flip and nowhere else, so a
        lane that never went live (dead feed, engine stopped during warmup) drops
        every position registered on it. That used to happen without a word.

        These are NOT closed out here. The engine never traded them, so booking a
        synthetic exit would destroy a position that is still legitimately open;
        the next start rebuilds it from claude_paper_fills. The only thing owed
        is saying so."""
        if not self._restore:
            return
        by_sleeve = {}
        for s in self.strategies:
            if id(s) in self._restore:
                pos, avg, _ = self._restore[id(s)]
                by_sleeve[self.label_of(s)] = f"{pos:+d} @ {avg:.2f}"
        log.warning("%d registered paper position(s) were NEVER APPLIED: the "
                    "lane never went live, so the warmup->live flip that hands "
                    "them back never ran. They are still open in "
                    "claude_paper_fills and NOT closed here -- the engine never "
                    "traded them. %s", len(self._restore), by_sleeve or "(unnamed)")


__all__ = ["LiveEngine"]
