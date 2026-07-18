"""JSON-line wire protocol shared by the platform bridges (NinjaTrader,
Quantower). One JSON object per line. Three channels:

  market  (platform -> engine): trade / quote / depth / bar / bookflow
  order   (engine -> platform): place / cancel / modify
  broker  (platform -> engine): fill / position / account

All timestamps are epoch NANOSECONDS. Side/aggressor: +1 buy/bid, -1 sell/ask.
Keeping the schema tiny keeps the C#/.NET relay trivial.
"""
from __future__ import annotations

import json

from ..core.events import (
    AccountUpdate,
    Bar,
    BookFlow,
    BrokerEvent,
    DepthUpdate,
    Fill,
    MarketEvent,
    PositionUpdate,
    Quote,
    Trade,
)
from ..core.orders import Order


def encode(obj: dict) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode()


def decode_market(m: dict, symbol: str = "") -> MarketEvent | None:
    """`symbol` is the receiving adapter's instrument lane (one socket = one
    instrument today). A "symbol" key in the wire message wins if present."""
    t = m.get("t")
    ts = int(m["ts"])
    sym = m.get("symbol", symbol)
    if t == "trade":
        return Trade(ts, float(m["price"]), int(m["size"]), int(m["aggressor"]), sym)
    if t == "quote":
        return Quote(ts, float(m["bid"]), float(m["ask"]), int(m["bid_size"]), int(m["ask_size"]), sym)
    if t == "depth":
        return DepthUpdate(ts, int(m["side"]), float(m["price"]), int(m["size"]), int(m.get("level", 0)), sym)
    if t == "bar":
        return Bar(ts, m["tf"], float(m["o"]), float(m["h"]), float(m["l"]), float(m["c"]), int(m["v"]), sym)
    if t == "bookflow":
        return BookFlow(ts, int(m["bid_cancel"]), int(m["ask_cancel"]),
                        int(m["bid_add"]), int(m["ask_add"]), sym)
    return None


def order_to_msg(o: Order) -> dict:
    return {
        "t": "place", "order_id": o.order_id, "symbol": o.symbol, "side": o.side,
        "qty": o.qty, "otype": o.type.value, "limit": o.limit_price, "stop": o.stop_price,
        "tif": o.tif.value, "tag": o.tag, "reduce_only": o.reduce_only,
    }


def cancel_msg(order_id: str) -> dict:
    return {"t": "cancel", "order_id": order_id}


def modify_msg(order_id: str, **changes) -> dict:
    return {"t": "modify", "order_id": order_id, **changes}


def decode_broker(m: dict) -> BrokerEvent | None:
    t = m.get("t")
    ts = int(m["ts"])
    if t == "fill":
        return Fill(ts, m["order_id"], m["symbol"], float(m["price"]), int(m["size"]),
                    float(m.get("commission", 0.0)), float(m.get("slippage", 0.0)), m.get("tag", ""))
    if t == "position":
        return PositionUpdate(ts, m["symbol"], int(m["qty"]), float(m["avg_px"]))
    if t == "account":
        return AccountUpdate(ts, float(m["equity"]), float(m["realized"]), float(m["unrealized"]))
    return None


__all__ = ["encode", "decode_market", "order_to_msg", "cancel_msg", "modify_msg", "decode_broker"]
