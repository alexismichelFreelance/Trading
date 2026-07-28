"""SweepFollowStrategy — follow deep aggressive sweeps, briefly.

Grounded in tools/flow_replication.py + tools/sweep_replication.py, measured on
44.5M real CME MBO trade messages and replicated out-of-sample on a second
contract and a different regime:

  * copying EVERY aggressive trade LOSES (-0.015pt/contract by 900s)
  * how many price levels an aggressor pays THROUGH beats how big it is, about
    2x at matched volume. ticks_spanned>=6 earned +0.58..+0.67pt/contract at 1s
    on both ESM5 and ESH5, ~90% sign consistency
  * the edge is TRANSIENT -- it decays and is gone by ~60s. The long-horizon
    persistence seen on ESM5 did NOT replicate on ESH5; it was a trend artifact.

Detection needs no order_id: clustering the tape by a 1ms gap plus side flip
reproduces the MBO parent-order grouping with 99.84% per-fill agreement
(tools/sweep_proxy_test.py), so this runs on the ordinary NT8 trade stream.
1ms is a measured optimum, not a guess -- 0ms fragments real sweeps (the edge
goes NEGATIVE at span>=8) and wider windows merge unrelated orders.

Because the edge is transient the sleeve is deliberately short-horizon and
flat-by-default: enter one lot in the sweep's direction once the cluster ENDS
(you cannot be inside the sweep -- the sweep is how you learn of it), hold a
fixed few seconds, exit. No pyramiding, no overnight, EOD flat.

NOT a validated sleeve: the signal replicated, but per-EVENT P&L net of crossing
costs is a much smaller number than the per-contract edge (see
tools/sweep_backtest.py). Paper only until its own forward record says otherwise.
"""
from __future__ import annotations

from ..core.events import Signal, Trade
from ..core.orders import Order
from ..core.timeutil import et_minute_of_day, et_session_date
from .base import BaseStrategy

RTH_START = 9 * 60 + 30
EOD_FLAT = 15 * 60 + 59
NS_MS = 1_000_000
TICK = 0.25


class SweepFollowStrategy(BaseStrategy):
    def __init__(self, symbol: str, *, min_span_ticks: int = 6,
                 gap_ms: int = 1, hold_s: float = 15.0,
                 stop_ticks: float = 8.0, max_entries: int = 40,
                 tick: float = TICK, mode: str = "fade",
                 retrace_frac: float = 1.0, peer_exit: tuple[str, ...] = (),
                 gamma=None) -> None:
        self.symbol = symbol
        self.gamma = gamma
        # mode="fade" (DEFAULT): trade AGAINST the sweep. Following it loses on
        #   every parameter tested -- by the time a sweep is visible price is at
        #   the far end of it, and the win rate is 21-41%. Fading replicated on
        #   BOTH contracts after pessimistic costs: median +0.50pt at span>=6 and
        #   +0.75pt at span>=8, win 54-60%, on ESM5 and ESH5 alike.
        # mode="follow": kept for the paper twin only. Measured to lose.
        self.mode = 1 if mode == "follow" else -1
        self.min_span = min_span_ticks
        self.gap_ns = gap_ms * NS_MS
        self.hold_ns = int(hold_s * 1_000_000_000)
        self.stop = stop_ticks * tick
        self.tick = tick
        self.max_entries = max_entries
        # ── REAL exit signals, as opposed to the stop/timeout insurance ──
        # T (thesis complete): the fade predicts price RETURNS across the span
        #   the sweep just covered, so the target is the sweep's own ORIGIN.
        #   Scaled by the signal itself -- not a tick count I picked.
        self.retrace_frac = retrace_frac
        # I (thesis invalidated) is handled in on_trade: a NEW sweep in the same
        #   direction as the one we faded means it was ignition, not exhaustion.
        # O (opposing peer): roster labels whose entry signals close us out.
        self.peer_exit = tuple(peer_exit)
        self.pos = 0
        self._day: str | None = None
        self._reset_session()

    def _reset_session(self) -> None:
        self._c_dir = 0             # current cluster: direction, extremes, last ts
        self._c_hi = self._c_lo = 0.0
        self._c_ts = 0
        self.entries = 0
        self.trade: dict | None = None

    def on_position(self, p) -> None:
        self.pos = p.qty

    def reset_for_live(self) -> None:
        self.trade = None
        self.pos = 0
        self._c_dir = 0

    # ── tape clustering + entry ──────────────────────────────────────────────
    def on_trade(self, t: Trade) -> list[Order]:
        day = et_session_date(t.ts)
        if day != self._day:
            self._day = day
            self._reset_session()
            if self.pos != 0:                    # never carry overnight
                side = 1 if self.pos > 0 else -1
                return [Order(self.symbol, -side, abs(self.pos), tag="safety-flat",
                              reduce_only=True)]
        m = et_minute_of_day(t.ts)
        if m >= EOD_FLAT:
            self._c_dir = 0
            return self._flatten("moc")
        if self.pos != 0 and self.trade is not None:
            out = self._manage(t)
            if out:
                return out

        d = 1 if t.aggressor > 0 else -1
        cont = (d == self._c_dir and (t.ts - self._c_ts) <= self.gap_ns)
        if cont:
            self._c_hi = max(self._c_hi, t.price)
            self._c_lo = min(self._c_lo, t.price)
            self._c_ts = t.ts
            return []
        # the previous cluster just ENDED -- evaluate it, then start a new one
        orders = self._on_cluster_end(t)
        self._c_dir, self._c_hi, self._c_lo, self._c_ts = d, t.price, t.price, t.ts
        return orders

    def _on_cluster_end(self, t: Trade) -> list[Order]:
        if self._c_dir == 0:
            return []
        span_now = round((self._c_hi - self._c_lo) / self.tick)
        if self.pos != 0 or self.trade is not None:
            if self._invalidated(span_now, self._c_dir):
                return self._flatten("resweep")
            return []
        if self.entries >= self.max_entries:
            return []
        if et_minute_of_day(t.ts) < RTH_START:
            return []
        span = round((self._c_hi - self._c_lo) / self.tick)
        if span < self.min_span:
            return []
        if not self.gamma_entry_ok(t.ts, "short"):
            return []
        d = self._c_dir * self.mode          # fade by default: against the sweep
        self.entries += 1
        # entry price is where the tape is NOW, after the sweep -- we were never
        # inside it, so the move through the book is not ours to book
        # T: the sweep ran hi->lo (or lo->hi); target the fraction of that span
        # we expect price to give back. This is the exit THESIS.
        span_px = self._c_hi - self._c_lo
        target = t.price + d * self.retrace_frac * span_px
        self.trade = {"dir": d, "entry": t.price, "t0": t.ts,
                      "target": target, "sweep_dir": self._c_dir}
        return [Order(self.symbol, d, 1, tag="entry-sweep")]

    # ── management ──────────────────────────────────────────────────────────
    # Ordered by KIND, not by convenience: the thesis is checked first, then
    # invalidation, and only then the insurance. A stop firing means we were
    # wrong; a target or an invalidation means we were RIGHT to be watching.
    def _manage(self, t: Trade) -> list[Order]:
        tr = self.trade
        d = tr["dir"]
        # T -- thesis complete: price gave back the sweep's span
        if (t.price - tr["target"]) * d >= 0:
            return self._flatten("target")
        # insurance, not a decision
        if (t.price - tr["entry"]) * d <= -self.stop:
            return self._flatten("stop")
        if t.ts - tr["t0"] >= self.hold_ns:
            return self._flatten("timeout")
        return []

    # ── I: a fresh sweep the SAME way as the one we faded ────────────────────
    def _invalidated(self, span: int, sweep_dir: int) -> bool:
        """We faded an exhaustion. Another deep sweep in that same direction
        says it was ignition instead -- the reason we are in the trade is gone,
        so leave now rather than wait for the stop to tell us."""
        tr = self.trade
        return (tr is not None and span >= self.min_span
                and sweep_dir == tr["sweep_dir"])

    # ── O: a peer signalled against us ───────────────────────────────────────
    def on_signal(self, e: Signal) -> list[Order]:
        """Exit when a watched peer OPENS against our position. Their entry is
        information we do not have: no single sleeve can see that another has
        just committed the other way."""
        if self.pos == 0 or self.trade is None or not self.peer_exit:
            return []
        if e.reduce_only or e.source not in self.peer_exit:
            return []                       # peers CLOSING tell us nothing
        if e.side == self.trade["dir"]:
            return []                       # agrees with us
        return self._flatten(f"peer-{e.source.split(':')[-1]}")

    def _flatten(self, why: str) -> list[Order]:
        if self.pos == 0 or self.trade is None:
            self.trade = None
            return []
        d = self.trade["dir"]
        qty = abs(self.pos)
        self.trade = None
        return [Order(self.symbol, -d, qty, tag=f"swp-{why}", reduce_only=True)]


__all__ = ["SweepFollowStrategy"]
