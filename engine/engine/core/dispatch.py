"""Shared event dispatch — used by both ReplayEngine and LiveEngine so the
routing of events to strategies is identical in replay and live."""
from __future__ import annotations

from .events import Bar, BookFlow, DepthUpdate, Fill, PositionUpdate, Quote, Trade
from .orders import Order


def dispatch_market(strategies, e) -> list[Order]:
    """Route one market event to each strategy's typed handler; collect orders."""
    out: list[Order] = []
    for s in strategies:
        if isinstance(e, Trade):
            out += s.on_trade(e) or []
        elif isinstance(e, BookFlow):
            out += s.on_bookflow(e) or []
        elif isinstance(e, Bar):
            out += s.on_bar(e) or []
        elif isinstance(e, Quote):
            out += s.on_quote(e) or []
        elif isinstance(e, DepthUpdate):
            out += s.on_depth(e) or []
    return out


def dispatch_broker(strategies, be) -> None:
    """Feed one broker event (Fill / PositionUpdate) back to each strategy."""
    for s in strategies:
        if isinstance(be, Fill):
            s.on_fill(be)
        elif isinstance(be, PositionUpdate):
            s.on_position(be)


__all__ = ["dispatch_market", "dispatch_broker"]
