"""Shared event dispatch — used by both ReplayEngine and LiveEngine so the
routing of events to strategies is identical in replay and live.

Symbol routing: an event stamped with a symbol goes only to strategies whose
`.symbol` matches. An empty symbol on either side means "broadcast" — this
keeps single-instrument feeds, synthetic tests, and the parity suite exactly
as they were before multi-instrument support."""
from __future__ import annotations

from .events import Bar, BookFlow, DepthUpdate, Fill, PositionUpdate, Quote, Trade
from .orders import Order


def _wants(s, sym: str) -> bool:
    ssym = getattr(s, "symbol", "")
    return not sym or not ssym or sym == ssym


def dispatch_market(strategies, e) -> list[Order]:
    """Route one market event to each same-symbol strategy's typed handler."""
    sym = getattr(e, "symbol", "")
    out: list[Order] = []
    for s in strategies:
        if not _wants(s, sym):
            continue
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
    """Feed one broker event (Fill / PositionUpdate) back to each same-symbol
    strategy. (LiveEngine routes fills owner-only instead — unchanged.)"""
    sym = getattr(be, "symbol", "")
    for s in strategies:
        if not _wants(s, sym):
            continue
        if isinstance(be, Fill):
            s.on_fill(be)
        elif isinstance(be, PositionUpdate):
            s.on_position(be)


__all__ = ["dispatch_market", "dispatch_broker"]
