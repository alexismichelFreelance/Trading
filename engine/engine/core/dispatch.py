"""Shared event dispatch — used by both ReplayEngine and LiveEngine so the
routing of events to strategies is identical in replay and live.

Symbol routing: an event stamped with a symbol goes only to strategies whose
`.symbol` matches. An empty symbol on either side means "broadcast" — this
keeps single-instrument feeds, synthetic tests, and the parity suite exactly
as they were before multi-instrument support."""
from __future__ import annotations

from .events import (Bar, BookFlow, DepthUpdate, Fill, PositionUpdate, Quote,
                     Signal, Trade)
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


def dispatch_signal(strategies, sig: Signal, emitter=None) -> list[Order]:
    """Broadcast one strategy's intent to its PEERS on the same lane.

    The emitter is excluded -- a sleeve must never react to its own signal, or
    a single order becomes a feedback loop. Same symbol routing as market
    events. Unlike dispatch_broker this carries no position state, so it cannot
    reintroduce the cross-sleeve duplicate-flatten bug that owner-only fill
    attribution was built to fix."""
    out: list[Order] = []
    for s in strategies:
        if s is emitter or not _wants(s, sig.symbol):
            continue
        # Strategy is a STRUCTURAL protocol -- a duck-typed strategy that never
        # opted into the peer channel simply has no on_signal. Skipping it keeps
        # this purely additive; requiring the method would break every existing
        # implementer that does not inherit BaseStrategy.
        fn = getattr(s, "on_signal", None)
        if fn is None:
            continue
        out += fn(sig) or []
    return out


__all__ = ["dispatch_market", "dispatch_broker", "dispatch_signal"]
