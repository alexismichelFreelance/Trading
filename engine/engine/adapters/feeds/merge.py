"""MergeFeed — deterministic k-way merge of N feeds into one ts-ordered stream.

Composition keeps ReplayEngine single-feed: a multi-instrument replay is just
`ReplayEngine(MergeFeed([es_feed, nq_feed]), ...)`. Ordering is total and
reproducible: ascending `ts`, ties broken by feed index (the order feeds were
passed in), then by arrival order within a feed. This tie-break is the ONLY
place multi-symbol replay ordering is decided — pinned by test_merge_feed.
"""
from __future__ import annotations

import heapq
from collections.abc import AsyncIterator

from ...core.events import MarketEvent


class MergeFeed:
    def __init__(self, feeds: list) -> None:
        if not feeds:
            raise ValueError("MergeFeed needs at least one feed")
        self.feeds = list(feeds)

    @property
    def finite(self) -> bool:
        """A merge is bounded only if EVERY leg is. One live leg means the
        merged stream must be treated as reconnectable."""
        return all(getattr(f, "finite", False) for f in self.feeds)

    async def stream(self) -> AsyncIterator[MarketEvent]:
        its = [f.stream() for f in self.feeds]
        heap: list[tuple[int, int, int, MarketEvent]] = []  # (ts, feed_idx, seq, ev)
        seq = 0
        for idx, it in enumerate(its):
            try:
                ev = await anext(it)
                heapq.heappush(heap, (ev.ts, idx, seq, ev))
                seq += 1
            except StopAsyncIteration:
                pass
        while heap:
            _, idx, _, ev = heapq.heappop(heap)
            yield ev
            try:
                nxt = await anext(its[idx])
                heapq.heappush(heap, (nxt.ts, idx, seq, nxt))
                seq += 1
            except StopAsyncIteration:
                pass


__all__ = ["MergeFeed"]
