"""MergeFeed — deterministic k-way merge of N feeds into one ts-ordered stream.

Composition keeps ReplayEngine single-feed: a multi-instrument replay is just
`ReplayEngine(MergeFeed([es_feed, nq_feed]), ...)`. Ordering is total and
reproducible: ascending `ts`, ties broken by feed index (the order feeds were
passed in), then by arrival order within a feed. This tie-break is the ONLY
place multi-symbol replay ordering is decided — pinned by test_merge_feed.

A LIVE leg that ends is a DISCONNECT, not an end of data. Serving the surviving
legs past that point hides it completely: LiveEngine only reconnects when
stream() returns, so a merge that outlives a dead leg means that instrument is
gone for the session while every counter above keeps climbing. So when the merge
is not finite, the first leg to end ends the whole stream, named in the log, and
the reconnect loop above rebuilds every leg. Bounded replay is unchanged — there,
legs finishing is how the replay ends.
"""
from __future__ import annotations

import heapq
import logging
from collections.abc import AsyncIterator

from ...core.events import MarketEvent

log = logging.getLogger("engine.merge")


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

    def _name(self, idx: int) -> str:
        f = self.feeds[idx]
        return getattr(f, "symbol", "") or f"feed[{idx}]"

    async def stream(self) -> AsyncIterator[MarketEvent]:
        live = not self.finite
        its = [f.stream() for f in self.feeds]
        heap: list[tuple[int, int, int, MarketEvent]] = []  # (ts, feed_idx, seq, ev)
        seq = 0
        for idx, it in enumerate(its):
            try:
                ev = await anext(it)
                heapq.heappush(heap, (ev.ts, idx, seq, ev))
                seq += 1
            except StopAsyncIteration:
                if live:
                    log.warning("MERGE leg %s produced nothing -- ending the "
                                "merged stream so every leg reconnects",
                                self._name(idx))
                    return
        while heap:
            _, idx, _, ev = heapq.heappop(heap)
            yield ev
            try:
                nxt = await anext(its[idx])
                heapq.heappush(heap, (nxt.ts, idx, seq, nxt))
                seq += 1
            except StopAsyncIteration:
                if live:
                    # A live leg ending is a dropped socket. Surviving legs must
                    # not paper over it -- see the module docstring.
                    log.warning("MERGE leg %s ended: treating as a DISCONNECT "
                                "and ending the merged stream so every leg "
                                "reconnects (surviving legs: %s)",
                                self._name(idx),
                                [self._name(i) for i in range(len(its)) if i != idx])
                    return


__all__ = ["MergeFeed"]
