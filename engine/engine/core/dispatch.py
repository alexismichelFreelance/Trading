"""Shared event dispatch — used by both ReplayEngine and LiveEngine so the
routing of events to strategies is identical in replay and live.

Symbol routing: an event stamped with a symbol goes only to strategies whose
`.symbol` matches. An empty symbol on either side means "broadcast" — this
keeps single-instrument feeds, synthetic tests, and the parity suite exactly
as they were before multi-instrument support.

FAULT ISOLATION. A strategy handler is arbitrary code that touches the DB, the
clock and the network. It can raise. Until 2026-08-05 nothing caught it, so one
sleeve throwing ended the loop for every sleeve after it, on that event and on
every event that followed.

That is what turned a hardware fault into abandoned positions. On 2026-08-04 the
NVMe controller hung (stornvme 11 + 129, then 45 paging errors and 567 NTFS
delayed-write failures over 47 minutes); QuestDB writes failed; a sink raised;
dispatch died. Paper fills stopped at 11:10:00 ET, the 15:59 session flat never
fired, and 21 sleeves were left holding into the close -- while the feed, the
recorders and the raw-capture thread carried on to 16:01 and the terminal
heartbeat showed nothing wrong.

So: the blast radius of a broken sleeve is that sleeve. A handler that raises is
caught, counted and named; one that keeps raising is disabled so it cannot
drown the log or burn the loop. Nothing is swallowed silently -- silence is how
this survived a whole session.
"""
from __future__ import annotations

import logging

from .events import (Bar, BookFlow, DepthUpdate, Fill, PositionUpdate, Quote,
                     Signal, Trade)
from .orders import Order

log = logging.getLogger("engine.dispatch")

# A sleeve that fails this many times in a row is taken out of the rotation.
# Small, because a handler raising repeatedly is broken, not unlucky -- and the
# engine must keep trading the other sleeves either way.
MAX_CONSECUTIVE_FAILURES = 5

_failures: dict[str, dict] = {}


def _key(s) -> str:
    return f"{getattr(s, 'label', None) or type(s).__name__}@{id(s):x}"


def reset_failures() -> None:
    """Clear the failure ledger (tests, and a fresh session)."""
    _failures.clear()


def strategy_failures() -> dict[str, dict]:
    """{strategy key: {count, consecutive, disabled, last}} — for the heartbeat
    and the end-of-session summary, so a dead sleeve is visible without reading
    the log."""
    return {k: dict(v) for k, v in _failures.items()}


def _disabled(s) -> bool:
    rec = _failures.get(_key(s))
    return bool(rec and rec["disabled"])


def _note_failure(s, ev, exc: BaseException) -> None:
    k = _key(s)
    rec = _failures.setdefault(k, {"count": 0, "consecutive": 0,
                                   "disabled": False, "last": ""})
    rec["count"] += 1
    rec["consecutive"] += 1
    rec["last"] = f"{type(exc).__name__}: {exc}"
    if rec["consecutive"] >= MAX_CONSECUTIVE_FAILURES and not rec["disabled"]:
        rec["disabled"] = True
        log.error("STRATEGY DISABLED %s after %d consecutive failures -- the "
                  "rest of the book keeps trading. Last error on %s: %s",
                  k, rec["consecutive"], type(ev).__name__, rec["last"])
    else:
        log.error("strategy %s raised on %s (%d consecutive, %d total): %s",
                  k, type(ev).__name__, rec["consecutive"], rec["count"],
                  rec["last"], exc_info=True)


def _note_success(s) -> None:
    rec = _failures.get(_key(s))
    if rec and rec["consecutive"]:
        rec["consecutive"] = 0


def _wants(s, sym: str) -> bool:
    ssym = getattr(s, "symbol", "")
    return not sym or not ssym or sym == ssym


def dispatch_market(strategies, e) -> list[Order]:
    """Route one market event to each same-symbol strategy's typed handler.

    A handler that raises costs its own orders for this event and nothing more.
    """
    sym = getattr(e, "symbol", "")
    out: list[Order] = []
    for s in strategies:
        if not _wants(s, sym) or _disabled(s):
            continue
        try:
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
        except Exception as ex:
            _note_failure(s, e, ex)
        else:
            _note_success(s)
    return out


def dispatch_broker(strategies, be) -> None:
    """Feed one broker event (Fill / PositionUpdate) back to each same-symbol
    strategy. (LiveEngine routes fills owner-only instead — unchanged.)

    Isolated for the same reason: a sleeve that cannot process its own fill must
    not stop every other sleeve from learning about theirs.
    """
    sym = getattr(be, "symbol", "")
    for s in strategies:
        if not _wants(s, sym) or _disabled(s):
            continue
        try:
            if isinstance(be, Fill):
                s.on_fill(be)
            elif isinstance(be, PositionUpdate):
                s.on_position(be)
        except Exception as ex:
            _note_failure(s, be, ex)
        else:
            _note_success(s)


def dispatch_signal(strategies, sig: Signal, emitter=None) -> list[Order]:
    """Broadcast one strategy's intent to its PEERS on the same lane.

    The emitter is excluded -- a sleeve must never react to its own signal, or
    a single order becomes a feedback loop. Same symbol routing as market
    events. Unlike dispatch_broker this carries no position state, so it cannot
    reintroduce the cross-sleeve duplicate-flatten bug that owner-only fill
    attribution was built to fix."""
    out: list[Order] = []
    for s in strategies:
        if s is emitter or not _wants(s, sig.symbol) or _disabled(s):
            continue
        # Strategy is a STRUCTURAL protocol -- a duck-typed strategy that never
        # opted into the peer channel simply has no on_signal. Skipping it keeps
        # this purely additive; requiring the method would break every existing
        # implementer that does not inherit BaseStrategy.
        fn = getattr(s, "on_signal", None)
        if fn is None:
            continue
        try:
            out += fn(sig) or []
        except Exception as ex:
            _note_failure(s, sig, ex)
        else:
            _note_success(s)
    return out


__all__ = ["dispatch_market", "dispatch_broker", "dispatch_signal",
           "strategy_failures", "reset_failures", "MAX_CONSECUTIVE_FAILURES"]
