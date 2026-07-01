"""Normalized event schema — the only language the core speaks.

ALL timestamps are int epoch **nanoseconds**, UTC.

Side / aggressor convention (matches the project's VERIFIED tape semantics:
signed delta = buy - sell):
    +1 = BUY  (buyer aggressor / bid side of the book)
    -1 = SELL (seller aggressor / ask side of the book)

Feed primitives (live adapters emit these from the raw feed):
    Trade        one aggressive execution.
    Quote        top-of-book bid/ask.
    DepthUpdate  one resting-book level change (N levels deep).
    Bar          a closed OHLCV bar of timeframe `tf`.

Aggregated primitive:
    BookFlow     gross resting-book add/cancel flow over an interval, split by
                 side. This is a first-class signal for these strategies (the
                 ignition book-confirm and the BOOK-healing exit read it). Live
                 adapters compute it by aggregating raw L3/DOM deltas per second
                 (exactly as the research SQL did from mbo_events); the replay
                 adapter reads it straight from the precomputed 1s feature table.

Inbound (broker -> strategy):
    Fill, PositionUpdate, AccountUpdate.
"""
from __future__ import annotations

from dataclasses import dataclass

# Side / aggressor constants
BUY = 1
SELL = -1
BID = 1   # DepthUpdate.side: bid side of the book
ASK = -1  # DepthUpdate.side: ask side of the book


@dataclass(frozen=True, slots=True)
class Trade:
    ts: int           # epoch ns, UTC
    price: float
    size: int         # contracts (>0)
    aggressor: int    # +1 buy, -1 sell


@dataclass(frozen=True, slots=True)
class Quote:
    ts: int
    bid: float
    ask: float
    bid_size: int
    ask_size: int


@dataclass(frozen=True, slots=True)
class DepthUpdate:
    ts: int
    side: int         # +1 bid, -1 ask
    price: float
    size: int         # new resting size at this level (0 = level removed)
    level: int        # 0 = top of book


@dataclass(frozen=True, slots=True)
class Bar:
    ts: int           # bar CLOSE time (epoch ns)
    tf: str           # '1m', '30m', '1h', ...
    o: float
    h: float
    l: float
    c: float
    v: int


@dataclass(frozen=True, slots=True)
class BookFlow:
    """Gross resting-book flow aggregated over an interval ending at `ts`."""
    ts: int
    bid_cancel: int   # size of resting BIDs cancelled
    ask_cancel: int   # size of resting ASKs cancelled
    bid_add: int      # size of resting BIDs added
    ask_add: int      # size of resting ASKs added


# ── inbound (broker -> strategy) ─────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Fill:
    ts: int
    order_id: str
    symbol: str
    price: float
    size: int         # SIGNED: +long / -short (the change to position)
    commission: float # $ for this fill
    slippage: float   # points of adverse slippage modelled on this fill
    tag: str = ""     # echoes the originating order's tag


@dataclass(frozen=True, slots=True)
class PositionUpdate:
    ts: int
    symbol: str
    qty: int          # signed net position
    avg_px: float


@dataclass(frozen=True, slots=True)
class AccountUpdate:
    ts: int
    equity: float
    realized: float
    unrealized: float


MarketEvent = Trade | Quote | DepthUpdate | Bar | BookFlow
BrokerEvent = Fill | PositionUpdate | AccountUpdate

__all__ = [
    "BUY", "SELL", "BID", "ASK",
    "Trade", "Quote", "DepthUpdate", "Bar", "BookFlow",
    "Fill", "PositionUpdate", "AccountUpdate",
    "MarketEvent", "BrokerEvent",
]
