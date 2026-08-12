"""SimBroker — internal fill model for replay.

Fill rules (per the plan):
  - MARKET: fills at the current reference price + adverse slippage.
  - LIMIT : rests; fills when price trades THROUGH the level, at the limit price
            (no slippage — you provided liquidity).
  - STOP  : rests; triggers when price trades through the level, then fills at
            the stop price + adverse slippage (becomes a market order).

Slippage and commission come from a pluggable fill model. `CleanFill` charges
nothing (used for the 1-contract ignition/flow parity, where a flat 0.517-pt
round-turn is applied at reporting time). `RegimeCostFill` (added for the sized
zone sleeve) charges NinjaTrader commission + regime-coherent slippage.

The broker exposes the sync replay hooks `on_market_event` / `drain` / `on_end`
used by ReplayEngine, and the async `submit/cancel/modify` of the BrokerAdapter
port. Position is tracked here for reduce-only logic and PositionUpdate events;
the authoritative P&L ledger lives in the Blotter.
"""
from __future__ import annotations

import logging

from ...core.events import (
    Bar,
    BookFlow,
    DepthUpdate,
    Fill,
    PositionUpdate,
    Quote,
    Trade,
)
from ...core.orders import Order, OrderType

log = logging.getLogger("engine.sim")


class CleanFill:
    """No slippage, no commission. Cost applied at reporting (flat round-turn)."""

    def slippage_points(self, order: Order, base_price: float, ctx: dict) -> float:
        return 0.0

    def commission_usd(self, qty: int, ctx: dict) -> float:
        return 0.0


class SimBroker:
    def __init__(self, symbol: str, clock, fill_model=None) -> None:
        self.symbol = symbol
        self.clock = clock
        self.fill = fill_model or CleanFill()
        self.qty = 0
        self.avg_px = 0.0
        self.ref_price: float | None = None
        self.ctx: dict = {}                       # carries vol_proxy etc. for slippage
        self._resting: dict[str, Order] = {}
        self._pending: list = []

    # ── replay-driver sync hooks ─────────────────────────────────────────
    def on_market_event(self, e) -> None:
        lo = hi = last = None
        if isinstance(e, Trade):
            lo = hi = last = e.price
        elif isinstance(e, Bar):
            lo, hi, last = e.l, e.h, e.c
        elif isinstance(e, Quote):
            last = lo = hi = 0.5 * (e.bid + e.ask)
        elif isinstance(e, (BookFlow, DepthUpdate)):
            return  # no tradeable price path
        if last is not None:
            self.ref_price = last
        if lo is None:
            return
        # match resting orders against the [lo, hi] path
        for oid in list(self._resting):
            o = self._resting.get(oid)
            if o is None:
                continue
            base = self._cross_price(o, lo, hi)
            if base is not None:
                del self._resting[oid]
                self._execute(o, base, is_limit=(o.type is OrderType.LIMIT))

    def drain(self) -> list:
        out = self._pending
        self._pending = []
        return out

    def on_end(self) -> None:
        self._resting.clear()

    # ── BrokerAdapter port (async) ───────────────────────────────────────
    async def submit(self, order: Order) -> None:
        qty = self._effective_qty(order)
        if qty <= 0:
            return
        if order.type is OrderType.MARKET:
            # A level-triggered exit prices at its level, not at ref_price (the
            # last close). Replay has to agree with live here or the scorecard
            # measures a different engine from the one that trades. See
            # Order.trigger_price.
            base = order.trigger_price if order.trigger_price is not None \
                else self.ref_price
            if base is None:
                log.warning("market order with no reference price; dropped: %s", order)
                return
            self._execute(order, base, is_limit=False, qty=qty)
        else:
            # marketable-on-arrival? fill immediately, else rest
            if self.ref_price is not None:
                base = self._cross_price(order, self.ref_price, self.ref_price)
                if base is not None:
                    self._execute(order, base, is_limit=(order.type is OrderType.LIMIT), qty=qty)
                    return
            self._resting[order.order_id] = order

    async def cancel(self, order_id: str) -> None:
        self._resting.pop(order_id, None)

    async def modify(self, order_id: str, **changes) -> None:
        o = self._resting.get(order_id)
        if o is None:
            return
        from dataclasses import replace
        self._resting[order_id] = replace(o, **changes)

    async def events(self):  # live-style; replay uses drain()
        for be in self.drain():
            yield be

    # ── internals ────────────────────────────────────────────────────────
    def _cross_price(self, o: Order, lo: float, hi: float) -> float | None:
        """Return the base fill price if order o is crossable in [lo,hi], else None."""
        if o.type is OrderType.LIMIT:
            if o.side > 0 and lo <= o.limit_price:
                return o.limit_price
            if o.side < 0 and hi >= o.limit_price:
                return o.limit_price
        elif o.type is OrderType.STOP:
            if o.side > 0 and hi >= o.stop_price:
                return o.stop_price
            if o.side < 0 and lo <= o.stop_price:
                return o.stop_price
        return None

    def _effective_qty(self, o: Order) -> int:
        if not o.reduce_only:
            return o.qty
        if self.qty == 0 or (o.side > 0) == (self.qty > 0):
            return 0                      # nothing to reduce / would add
        return min(o.qty, abs(self.qty))

    def _execute(self, o: Order, base_price: float, is_limit: bool, qty: int | None = None) -> None:
        qty = o.qty if qty is None else qty
        slip = 0.0 if is_limit else self.fill.slippage_points(o, base_price, self.ctx)
        fill_px = base_price + o.side * slip
        comm = self.fill.commission_usd(qty, self.ctx)
        signed = o.side * qty
        self._apply_position(signed, fill_px)
        ts = self.clock.now()
        self._pending.append(Fill(ts, o.order_id, o.symbol, fill_px, signed, comm, slip, o.tag))
        self._pending.append(PositionUpdate(ts, o.symbol, self.qty, self.avg_px))

    def _apply_position(self, q: int, px: float) -> None:
        if self.qty == 0:
            self.qty, self.avg_px = q, px
        elif (q > 0) == (self.qty > 0):
            self.avg_px = (self.avg_px * self.qty + px * q) / (self.qty + q)
            self.qty += q
        elif abs(q) < abs(self.qty):
            self.qty += q                 # partial reduce, avg unchanged
        elif abs(q) == abs(self.qty):
            self.qty, self.avg_px = 0, 0.0
        else:
            self.qty += q                 # flipped through zero
            self.avg_px = px


__all__ = ["SimBroker", "CleanFill"]
