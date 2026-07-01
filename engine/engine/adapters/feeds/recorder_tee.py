"""RecorderTee — Phase 2 STUB. Wraps any live feed and writes the normalized
event stream into QuestDB as new historical data (forward-only library building,
so you stop re-buying data). Transparent: yields every event onward unchanged.

INTEGRATION: buffer normalized events and batch-INSERT into QuestDB tables
mirroring claude_sec_feat (per-second aggregates) and claude_bars_1m. Use
fire-and-forget INSERTs with per-day count verification (the project's proven
pattern for the WAL-commit race). See docs/PHASE2_BRIDGES.md.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from ...core.events import MarketEvent
from ..questdb import AsyncQuestDB


class RecorderTee:
    def __init__(self, inner, qdb: AsyncQuestDB | None = None,
                 symbol: str = "ES") -> None:
        self.inner = inner
        self.qdb = qdb or AsyncQuestDB()
        self.symbol = symbol

    async def stream(self) -> AsyncIterator[MarketEvent]:
        async for ev in self.inner.stream():
            # TODO(Phase 2): aggregate into 1s features / 1m bars and batch-insert
            # into QuestDB (fire-and-forget + per-day count verification).
            yield ev


__all__ = ["RecorderTee"]
