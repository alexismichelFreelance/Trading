"""
book.py — Incremental L3 order book reconstructed from MBO events.

Maintains a price-level view (aggregated sizes) used by the feature layer.
Handles Add / Cancel / Modify / Trade / Fill / Clear (R) actions.

Change from original:
    BookSnapshot now carries a `trades` field — Dict[float, int] —
    recording how much volume traded AT each price level during the
    second leading up to the snapshot.  This is the only addition;
    everything else is identical to the original.  The wall detector
    uses trades to distinguish "wall consumed by fills" from
    "wall cancelled quietly" — these look identical in size deltas
    alone but have completely different trading significance.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Order:
    order_id: int
    side: str        # 'B' or 'A'
    price: float
    size: int


@dataclass
class BookSnapshot:
    """Aggregated price-level view at a point in time."""
    ts:     pd.Timestamp
    bids:   Dict[float, int]   # price → total resting size
    asks:   Dict[float, int]   # price → total resting size
    trades: Dict[float, int]   # price → volume traded THIS second  ← NEW

    # ── convenience helpers ──────────────────────────────────────────────────

    def best_bid(self) -> Optional[float]:
        return max(self.bids.keys()) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks.keys()) if self.asks else None

    def mid(self) -> Optional[float]:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is not None and ba is not None:
            return (bb + ba) / 2.0
        return None

    def depth_bid(self, n_levels: int) -> list[Tuple[float, int]]:
        """Top N bid levels sorted best → worst."""
        sorted_prices = sorted(self.bids.keys(), reverse=True)[:n_levels]
        return [(p, self.bids[p]) for p in sorted_prices]

    def depth_ask(self, n_levels: int) -> list[Tuple[float, int]]:
        """Top N ask levels sorted best → worst."""
        sorted_prices = sorted(self.asks.keys())[:n_levels]
        return [(p, self.asks[p]) for p in sorted_prices]

    def total_bid_size(self, n_levels: int) -> int:
        return sum(s for _, s in self.depth_bid(n_levels))

    def total_ask_size(self, n_levels: int) -> int:
        return sum(s for _, s in self.depth_ask(n_levels))

    def traded_at(self, price: float) -> int:
        """Volume traded at exactly this price level this second."""
        return self.trades.get(price, 0)

    def total_traded(self) -> int:
        """Total volume traded across all levels this second."""
        return sum(self.trades.values())


# ── Book engine ───────────────────────────────────────────────────────────────

class L3Book:
    """
    Stateful incremental order book for a single symbol.

    Call .apply(row) for every MBO event row (as a namedtuple or dict-like).
    Call .snapshot() to get the current aggregated view.
    """

    def __init__(self) -> None:
        self._orders:   Dict[int, Order]   = {}
        self._bids:     Dict[float, int]   = defaultdict(int)
        self._asks:     Dict[float, int]   = defaultdict(int)
        self._trades:   Dict[float, int]   = defaultdict(int)  # NEW: reset each second
        self._last_ts:  Optional[pd.Timestamp] = None

    # ── public API ────────────────────────────────────────────────────────────

    def apply(self, row) -> None:
        """Process one MBO event row."""
        action   = str(row.action).upper()
        side     = str(row.side).upper()
        price    = float(row.price)
        size     = int(row.size)
        order_id = int(row.order_id)
        self._last_ts = row.ts_recv

        if action == "A":
            self._add(order_id, side, price, size)
        elif action == "C":
            self._cancel(order_id)
        elif action == "M":
            self._modify(order_id, price, size)
        elif action in ("T", "F"):
            self._trade(order_id, size)
        elif action == "R":
            self._clear()

    def snapshot(self) -> BookSnapshot:
        """Return immutable snapshot of current state, then reset trade accumulator."""
        bids   = {p: s for p, s in self._bids.items()   if s > 0}
        asks   = {p: s for p, s in self._asks.items()   if s > 0}
        trades = dict(self._trades)   # copy before reset
        self._trades.clear()          # reset for next second
        return BookSnapshot(ts=self._last_ts, bids=bids, asks=asks, trades=trades)

    def reset(self) -> None:
        self._orders.clear()
        self._bids.clear()
        self._asks.clear()
        self._trades.clear()

    # ── private helpers ───────────────────────────────────────────────────────

    def _levels(self, side: str) -> Dict[float, int]:
        return self._bids if side == "B" else self._asks

    def _add(self, order_id: int, side: str, price: float, size: int) -> None:
        if order_id in self._orders:
            self._cancel(order_id)
        order = Order(order_id=order_id, side=side, price=price, size=size)
        self._orders[order_id] = order
        self._levels(side)[price] += size

    def _cancel(self, order_id: int) -> None:
        order = self._orders.pop(order_id, None)
        if order is None:
            return
        lvls = self._levels(order.side)
        lvls[order.price] -= order.size
        if lvls[order.price] <= 0:
            lvls.pop(order.price, None)

    def _modify(self, order_id: int, new_price: float, new_size: int) -> None:
        order = self._orders.get(order_id)
        if order is None:
            log.debug("Modify on unknown order_id=%d — skipping", order_id)
            return
        lvls = self._levels(order.side)
        lvls[order.price] -= order.size
        if lvls[order.price] <= 0:
            lvls.pop(order.price, None)
        order.price = new_price
        order.size  = new_size
        lvls[new_price] += new_size

    def _trade(self, order_id: int, traded_size: int) -> None:
        """A trade reduced the resting order by traded_size."""
        order = self._orders.get(order_id)
        if order is None:
            return
        lvls = self._levels(order.side)
        fill = min(traded_size, order.size)
        order.size -= fill
        lvls[order.price] -= fill
        if lvls[order.price] <= 0:
            lvls.pop(order.price, None)
        if order.size <= 0:
            self._orders.pop(order_id, None)
        # ── NEW: accumulate trade volume at this price ──
        self._trades[order.price] += fill

    def _clear(self) -> None:
        self._orders.clear()
        self._bids.clear()
        self._asks.clear()
        self._trades.clear()


# ── Snapshot series builder ───────────────────────────────────────────────────

def build_snapshots(
    df: pd.DataFrame,
    freq: str = "1s",
) -> list[BookSnapshot]:
    """
    Replay all MBO events in *df* and capture a BookSnapshot at every
    *freq* boundary.  Returns a list sorted by ts.

    df must have columns: ts_recv, action, side, price, size, order_id
    and be sorted by ts_recv ascending.

    The trades dict in each snapshot records fills that occurred in the
    interval (prev_boundary, this_boundary].
    """
    book = L3Book()
    snapshots: list[BookSnapshot] = []

    if df.empty:
        return snapshots

    start = df["ts_recv"].iloc[0].floor(freq)
    end   = df["ts_recv"].iloc[-1].ceil(freq)
    boundaries = pd.date_range(start, end, freq=freq, tz="UTC")

    boundary_idx  = 0
    n_boundaries  = len(boundaries)

    for row in df.itertuples(index=False):
        # Emit snapshots for all boundaries before this event
        while boundary_idx < n_boundaries and boundaries[boundary_idx] <= row.ts_recv:
            snap = book.snapshot()   # also resets trade accumulator
            snapshots.append(BookSnapshot(
                ts     = boundaries[boundary_idx],
                bids   = snap.bids,
                asks   = snap.asks,
                trades = snap.trades,
            ))
            boundary_idx += 1
        book.apply(row)

    # Final snapshot
    if boundary_idx < n_boundaries:
        snapshots.append(book.snapshot())

    return snapshots
