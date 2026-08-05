"""ReplayFeed emits prices that cannot exist in the market.

claude_sec_feat.pxc is not a price -- it is the VOLUME-WEIGHTED AVERAGE of the
second. Verified against the raw tape (mbo_events) on ESM5 2025-05-05 RTH,
21,183 seconds:

    pxc on the 0.25 tick grid       0.00%   (not one valid price all session)
    |pxc - last traded price| > 1t  21.5%
    |pxc - last traded price| > 2t   7.8%
    max deviation                   2.118pt (8.5 ticks)

ReplayFeed builds every Trade at float(pxc), so a strategy replayed through it
enters and exits at a sub-tick average that no order could ever have been filled
at. Two consequences, both bad:

  * the fill price is off the tick grid ALWAYS -- an impossible print;
  * a VWAP sits INSIDE the second's own range 53% of the time, so a round trip
    executed there systematically avoids the spread it would have had to pay.
    The replay is not pessimistic-but-close, it is optimistic by construction.

Rounding to the instrument tick does not make pxc the right price -- it is still
an average, not the last trade -- but it removes the impossible-print class of
error and stops the replay reporting fills at prices that never existed. The
residual VWAP-vs-last-trade bias is documented in ReplayFeed's docstring.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.feeds.replay_questdb import ReplayFeed  # noqa: E402
from engine.core.events import Trade  # noqa: E402

TICK = 0.25
# real pxc values pulled from claude_sec_feat, ESM5 2025-05-05 14:30 UTC
VWAP_PRICES = [5675.954551, 5676.257328, 5675.807830, 5675.807297,
               5675.905194, 5675.932432, 5675.659159, 5675.725323]


class FakeQDB:
    """Serves one session of per-second rows shaped like claude_sec_feat."""

    def __init__(self, prices: list[float]) -> None:
        self._px = prices

    async def df(self, sql: str) -> pd.DataFrame:
        if "DISTINCT day" in sql:
            return pd.DataFrame({"day": [pd.Timestamp("2025-05-05")]})
        if "claude_bars_1m" in sql or " o, h, l, c" in sql:
            return pd.DataFrame(columns=["ts", "o", "h", "l", "c", "vol"])
        n = len(self._px)
        base = pd.Timestamp("2025-05-05T14:30:00Z").value
        return pd.DataFrame({
            "ts": pd.to_datetime([base + i * 1_000_000_000 for i in range(n)]),
            "pxc": self._px,
            "adelta": [4] * n, "avol": [20] * n,
            "bid_cancel": [1] * n, "ask_cancel": [1] * n,
            "bid_add": [1] * n, "ask_add": [1] * n,
        })


def _drain(feed: ReplayFeed) -> list[Trade]:
    async def go():
        return [e async for e in feed.stream() if isinstance(e, Trade)]
    return asyncio.run(go())


def _on_grid(p: float) -> bool:
    return abs(p / TICK - round(p / TICK)) < 1e-9


def test_replayed_trade_prices_are_on_the_tick_grid():
    """THE REPRODUCTION. Every replayed fill price must be a price that could
    actually have printed. On the broken feed none of them are."""
    trades = _drain(ReplayFeed(FakeQDB(VWAP_PRICES), symbol="ESM5", tick=TICK))
    assert trades, "feed produced no trades; test did not exercise the path"
    bad = [t.price for t in trades if not _on_grid(t.price)]
    assert not bad, (
        f"{len(bad)} of {len(trades)} replayed prices are off the {TICK} tick "
        f"grid and could never have traded, e.g. {bad[:3]}")


def test_rounding_moves_price_by_less_than_one_tick():
    """The fix must not relocate the trade -- only snap it to a legal price."""
    trades = _drain(ReplayFeed(FakeQDB(VWAP_PRICES), symbol="ESM5", tick=TICK))
    for t, raw in zip(trades[::2], VWAP_PRICES):     # one BUY per second
        assert abs(t.price - raw) <= TICK / 2 + 1e-9, (
            f"rounded {raw} to {t.price}: moved more than half a tick")


def test_default_tick_leaves_prices_untouched():
    """tick=0 disables snapping, so existing callers that pass no tick keep
    byte-identical behaviour and no parity baseline moves silently."""
    trades = _drain(ReplayFeed(FakeQDB(VWAP_PRICES), symbol="ESM5"))
    got = sorted({t.price for t in trades})
    assert got == sorted(set(VWAP_PRICES)), "unticked feed must not alter prices"


def test_buy_and_sell_of_the_same_second_share_one_price():
    """Both sides of a second are priced from the same pxc, so snapping must not
    split them -- otherwise the replay invents a spread that was never there."""
    trades = _drain(ReplayFeed(FakeQDB(VWAP_PRICES[:3]), symbol="ESM5", tick=TICK))
    by_ts: dict[int, set[float]] = {}
    for t in trades:
        by_ts.setdefault(t.ts, set()).add(t.price)
    for ts, prices in by_ts.items():
        assert len(prices) == 1, f"second {ts} priced two ways: {prices}"
