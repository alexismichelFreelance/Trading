"""ReplayFeed — streams QuestDB history as normalized events in ts order.

Per-second flow (claude_sec_feat / claude_sec_feat_esh5) -> reconstructed BUY/
SELL Trades + a BookFlow per second (the end-of-second marker). Optionally
merges 1-minute Bars from claude_bars_1m (for the 30m zone / 1h HMM pipeline):
each 1m bar is emitted at its CLOSE time (bucket-start + 60s) so it is strictly
causal, and ordered BEFORE the same-second flow so coarse features update first.

The forward-looking hi/lo/nxt columns are NEVER read.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from ...core.events import BUY, SELL, Bar, BookFlow, MarketEvent, Trade
from ..questdb import AsyncQuestDB

_SEC_COLS = "ts, pxc, adelta, avol, bid_cancel, ask_cancel, bid_add, ask_add"
_MIN_NS = 60 * 1_000_000_000


class ReplayFeed:
    def __init__(self, qdb: AsyncQuestDB, table: str = "claude_sec_feat",
                 start: str | None = None, end: str | None = None,
                 days: list[str] | None = None, bars_symbol: str | None = None,
                 bars_table: str = "claude_bars_1m", symbol: str = "",
                 tick: float = 0.0) -> None:
        self.qdb = qdb
        self.table = table
        # `pxc` is the VOLUME-WEIGHTED AVERAGE of the second, not a traded price.
        # Measured against the raw tape (mbo_events, ESM5 2025-05-05 RTH, 21,183
        # seconds): 0.00% of pxc values sit on the 0.25 tick grid, 21.5% are more
        # than a tick from the last traded price, max deviation 2.118pt. Replaying
        # fills at that value books trades at prints that never existed, and
        # because a VWAP lies inside the second's own range 53% of the time, a
        # round trip there silently avoids the spread it should have paid.
        # tick>0 snaps to the nearest legal price. It does NOT make pxc the right
        # price -- it is still an average rather than the last trade -- but it
        # removes the impossible-print error. Default 0.0 leaves existing callers
        # byte-identical so no parity baseline shifts without being asked for.
        self.tick = tick
        self.start = start
        self.end = end
        self._days = days
        self.bars_symbol = bars_symbol
        self.bars_table = bars_table
        # instrument lane stamped on every emitted event (contract, e.g. 'ESM5');
        # defaults to bars_symbol so existing callers get stamping for free.
        self.symbol = symbol or (bars_symbol or "")

    def _range_where(self, col: str = "ts") -> str:
        cl = []
        if self.start:
            cl.append(f"{col} >= '{self.start}T00:00:00.000000Z'")
        if self.end:
            cl.append(f"{col} < '{self.end}T00:00:00.000000Z'")
        return (" WHERE " + " AND ".join(cl)) if cl else ""

    async def list_days(self) -> list[str]:
        if self._days is not None:
            return list(self._days)
        df = await self.qdb.df(f"SELECT DISTINCT day FROM {self.table}{self._range_where()} ORDER BY day")
        return [d.strftime("%Y-%m-%d") for d in df["day"]]

    async def _sec_events(self, day: str) -> list[tuple[int, int, MarketEvent]]:
        df = await self.qdb.df(
            f"SELECT {_SEC_COLS} FROM {self.table} "
            f"WHERE day = '{day}T00:00:00.000000Z' ORDER BY ts")
        ts = df["ts"].astype("int64").to_numpy()
        pxc = df["pxc"].to_numpy()
        adelta = df["adelta"].astype("int64").to_numpy()
        avol = df["avol"].astype("int64").to_numpy()
        bc = df["bid_cancel"].astype("int64").to_numpy()
        ac = df["ask_cancel"].astype("int64").to_numpy()
        ba = df["bid_add"].astype("int64").to_numpy()
        aa = df["ask_add"].astype("int64").to_numpy()
        sym = self.symbol
        out: list[tuple[int, int, MarketEvent]] = []
        for i in range(len(df)):
            t = int(ts[i])
            ad, av, px = int(adelta[i]), int(avol[i]), pxc[i]
            if px == px and self.tick > 0:
                # snap once, before the split, so the BUY and the SELL of the
                # same second keep one price and no phantom spread is invented
                px = round(px / self.tick) * self.tick
            if px == px:  # not NaN
                buy = (av + ad) // 2
                sell = (av - ad) // 2
                if buy > 0:
                    out.append((t, 1, Trade(t, float(px), int(buy), BUY, sym)))
                if sell > 0:
                    out.append((t, 1, Trade(t, float(px), int(sell), SELL, sym)))
            out.append((t, 2, BookFlow(t, int(bc[i]), int(ac[i]), int(ba[i]), int(aa[i]), sym)))
        return out

    async def _bar_events(self, day: str) -> list[tuple[int, int, MarketEvent]]:
        if not self.bars_symbol:
            return []
        df = await self.qdb.df(
            f"SELECT ts, o, h, l, c, vol FROM {self.bars_table} "
            f"WHERE symbol = '{self.bars_symbol}' "
            f"AND ts >= '{day}T00:00:00.000000Z' AND ts < '{day}T23:59:59.999999Z' ORDER BY ts")
        ts = df["ts"].astype("int64").to_numpy()
        sym = self.symbol
        out = []
        for i in range(len(df)):
            close_ts = int(ts[i]) + _MIN_NS    # emit at bar close (causal)
            out.append((close_ts, 0, Bar(close_ts, "1m", float(df["o"].iloc[i]),
                                         float(df["h"].iloc[i]), float(df["l"].iloc[i]),
                                         float(df["c"].iloc[i]), int(df["vol"].iloc[i]), sym)))
        return out

    async def stream(self) -> AsyncIterator[MarketEvent]:
        for day in await self.list_days():
            events = await self._sec_events(day)
            events += await self._bar_events(day)
            events.sort(key=lambda x: (x[0], x[1]))   # stable: (ts, priority)
            for _, _, ev in events:
                yield ev


__all__ = ["ReplayFeed"]
