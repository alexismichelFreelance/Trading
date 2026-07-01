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
