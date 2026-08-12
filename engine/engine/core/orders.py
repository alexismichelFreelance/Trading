"""Order types the core emits and the broker adapters consume."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class TIF(str, Enum):
    DAY = "DAY"     # cancel at session end
    GTC = "GTC"
    IOC = "IOC"     # fill now or cancel


_oid = itertools.count(1)


def next_order_id() -> str:
    return f"O{next(_oid)}"


@dataclass(frozen=True, slots=True)
class Order:
    symbol: str
    side: int                       # +1 buy, -1 sell
    qty: int                        # absolute number of contracts (>0)
    type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    tif: TIF = TIF.DAY
    tag: str = ""                   # strategy label: entry/scaleout/stop/target/exit
    reduce_only: bool = False       # only reduces an existing position
    order_id: str = field(default_factory=next_order_id)
    # The PRICE THIS EXIT WAS TRIGGERED AT, for a MARKET order that a strategy
    # sent because the tape reached a level it was already carrying (a stop, a
    # target, a scale-out). Such an order is "at the market" only in the sense
    # that it must not rest; it is not priced at the market. Detection happens
    # on the bar, so without this the fill takes the engine's last price -- the
    # bar's CLOSE -- and a stop tripped by a wick that closed back books BETTER
    # than the stop. Measured live 2026-08-12: ES:pivot long 7765.25, next bar
    # low 7757.50 through the stop, close 7769.25, booked +4.00 on a stopped-out
    # trade. The strategy holds the bar and therefore knows the honest fill: the
    # level, or the bar's OPEN when it gapped past the level (real slippage).
    # None = a genuine market exit (timeout, session flat) -- priced at the
    # market, unchanged.
    trigger_price: float | None = None

    def __post_init__(self) -> None:
        if self.side not in (1, -1):
            raise ValueError(f"side must be +1/-1, got {self.side}")
        if self.qty <= 0:
            raise ValueError(f"qty must be > 0, got {self.qty}")
        if self.type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("LIMIT order requires limit_price")
        if self.type is OrderType.STOP and self.stop_price is None:
            raise ValueError("STOP order requires stop_price")


__all__ = ["OrderType", "TIF", "Order", "next_order_id"]
