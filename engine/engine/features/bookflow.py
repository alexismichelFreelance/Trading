"""BookFlowAggregator — derive per-second gross resting-book add/cancel flow
from a live DOM (DepthUpdate) stream, so live strategies see the same `BookFlow`
signal that replay reads from the precomputed 1s table.

Per (side, price) level, a size increase counts as an ADD, a size decrease as a
CANCEL; summed per side per second. This is the L2-net view of the L3 add/cancel
the research used from raw mbo_events — a close, causal approximation (if the NT
add-on forwards raw depth Operation add/remove it can be made exact; see
docs/PHASE2_BRIDGES.md). The feed calls `accumulate` on each depth tick and
`snapshot(sec)` at each 1-second boundary.
"""
from __future__ import annotations

from ..core.events import BID, BookFlow, DepthUpdate

NS = 1_000_000_000


class BookFlowAggregator:
    def __init__(self) -> None:
        self._size: dict[tuple[int, float], int] = {}
        # accumulators for the current second: [bid_cancel, ask_cancel, bid_add, ask_add]
        self._acc = [0, 0, 0, 0]

    def accumulate(self, du: DepthUpdate) -> None:
        key = (du.side, du.price)
        prev = self._size.get(key, 0)
        delta = du.size - prev
        if du.size <= 0:
            self._size.pop(key, None)
        else:
            self._size[key] = du.size
        if delta > 0:                         # add
            self._acc[2 if du.side == BID else 3] += delta
        elif delta < 0:                       # cancel
            self._acc[0 if du.side == BID else 1] += -delta

    def snapshot(self, sec: int) -> BookFlow:
        bf = BookFlow(sec * NS, self._acc[0], self._acc[1], self._acc[2], self._acc[3])
        self._acc = [0, 0, 0, 0]
        return bf


__all__ = ["BookFlowAggregator"]
