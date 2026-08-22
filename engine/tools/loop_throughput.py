"""How many events per second can the LIVE ENGINE LOOP process? Not the sleeves.

tools/dispatch_throughput.py calls dispatch_market() directly. That measures
strategy compute and NOTHING ELSE -- it never constructs a LiveEngine, so every
per-event thing the real loop does is invisible to it:

    _flatten_for_session(sym, ts)     awaited on EVERY event, once live
    _check_resting(sym, px, ts)       awaited on EVERY event with anything resting
    risk.on_market(ts, px, books)     EVERY event, rebuilding a book list
    _paper_submit / _submit_owned     awaited per order
    _emit_sink(...)                   per bar, per fill, per order

It reported 722 trades/s on 2026-08-17 while the real engine was draining
0.26-2.1 events/s. Both numbers are correct; they measure different things, and
the one that mattered was never taken.

WHY THE RATE IS KNOWN. The live alarm calls _note_lag once per 1024 processed
events, so the SPACING of those log lines is a direct read of the drain rate:
15:45 -> 16:50 is 65 minutes for 1024 events = 0.26 events/s. Overnight the
engine sat at lag 600.0s with queue 0; it broke at the 09:30 ET cash open and
never recovered, ending 245 minutes behind with 47,991 events queued.

THE VARIABLE THIS ISOLATES. Overnight the engine is FLAT. After the open it
holds positions and carries resting stops and targets, and _check_resting is
awaited on every single event once _resting is non-empty. So this measures the
loop twice -- flat, then holding -- because the difference between those two is
the difference between the healthy hours and the broken ones.

    .venv/Scripts/python.exe tools/loop_throughput.py [n_events] [symbol]

This is a MEASUREMENT, not a fix. Nothing here is a diagnosis until it either
reproduces the slowdown or rules the loop out.

RESULT, 2026-08-18, 28 sleeves, ES:

    loop, FLAT                     ~2,360 ev/s
    loop, HOLDING (56 resting)      1,788 ev/s     (holding costs 1.32x)

IT RULES THE LOOP OUT. Against a live drain of 0.26-2.1 ev/s that is three
orders of magnitude of headroom, and holding positions -- the one thing that
separates the healthy overnight hours from the broken RTH ones -- costs 1.32x,
not 1000x. Neither the sleeves (722/s, dispatch_throughput.py) nor the loop is
the bottleneck.

What this harness does NOT contain is now the whole remaining suspect list,
because it is everything the live process has that this does not:

  * rawcapture, IN-PROCESS on its own threads, 7.36M rows on 2026-08-17 over
    ILP :9009. Python work holding the GIL at 400+ rows/s at the open, which is
    exactly when the engine broke. Its ILP writer logged repeated
    "reconnect: timed out" from 16:58 on.
  * the painter's NT8 draw socket and the paper blotter's QuestDB writes,
    reached through the sink queue (which never dropped a callback -- so the
    sinks were keeping UP, and are the weaker half of this list).
  * the real NinjaTrader feed and broker adapters, replaced here by ListFeed
    and NullBroker.

The GIL story fits a fact nothing else explains: total log volume HALVED at the
open, and that includes rawcapture's own timer-driven lines, which come from a
DIFFERENT THREAD. A blocked event loop cannot slow another thread down; a
saturated GIL slows every thread in the process at once.

Next measurement, not next fix: run this loop with rawcapture live in-process at
open volume and see whether 1,788 ev/s collapses.
"""
from __future__ import annotations

import asyncio
import cProfile
import io
import pstats
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from engine.core.blotter import Blotter              # noqa: E402
from engine.core.clock import WallClock              # noqa: E402
from engine.core.live_engine import LiveEngine       # noqa: E402
from engine.core.orders import Order, OrderType      # noqa: E402
from engine.features.gamma_curve import GammaCurve   # noqa: E402

import dispatch_throughput as dt                     # noqa: E402
import run_live                                      # noqa: E402


class ListFeed:
    """finite=True so run() ends on its own instead of waiting for a reconnect."""
    finite = True

    def __init__(self, ev):
        self._ev = ev

    async def stream(self):
        for e in self._ev:
            yield e


class NullBroker:
    async def submit(self, o):
        return None

    async def events(self):
        return
        yield


def _roster(symbol: str):
    r = run_live.build_roster("all", symbol=symbol)
    st = [s for _, s in r] if isinstance(r[0], tuple) else list(r)
    rows = [(7000.0 + i * 5.0, 10.0 - abs(i - 120) * 0.05,
             -(10.0 - abs(i - 110) * 0.05)) for i in range(235)]
    curve = GammaCurve.from_rows(rows, spot=7600.0, basis=20.0, underlying="SPX")
    run_live.attach_curves(st, symbol, "2026-08-11", curve=curve)
    return st


PV = {"ES": 50.0, "NQ": 20.0}


def _engine(strategies, events, symbol="ES"):
    # live_owners=set(): every sleeve is PAPER, so orders take the inline
    # _paper_submit path. That is the real roster's shape (22 of 28 are paper)
    # and it is the path that runs inside the dispatch loop.
    eng = LiveEngine(ListFeed(events), NullBroker(), strategies,
                     WallClock(), Blotter(symbol, PV.get(symbol, 50.0)),
                     warmup_gate=False, live_owners=set(), drain_timeout=0.05)
    eng.lag_report_s = 0.0            # measuring, not alarming
    return eng


def _seed_positions(eng, strategies, symbol: str, px: float = 7700.0) -> int:
    """Put the engine in the state the LIVE engine was in after the open:
    holding, with protective orders resting. Seven sleeves held a position on
    2026-08-17; this seeds every sleeve on the lane so the per-event cost of
    _check_resting is measured at a realistic depth, not at one."""
    n = 0
    for s in strategies:
        if getattr(s, "symbol", "") not in (symbol, ""):
            continue
        eng._spos[id(s)] = 1
        eng._resting.append((s, Order(symbol, -1, 1, type=OrderType.STOP,
                                      stop_price=px - 12.0, reduce_only=True)))
        eng._resting.append((s, Order(symbol, -1, 1, type=OrderType.LIMIT,
                                      limit_price=px + 20.0, reduce_only=True)))
        n += 2
    return n


def _run(label: str, strategies, events, symbol="ES") -> float:
    eng = _engine(strategies, events, symbol)
    t0 = time.perf_counter()
    asyncio.run(eng.run())
    d = time.perf_counter() - t0
    rate = len(events) / d
    print(f"{label:34s} {rate:9.1f} ev/s   ({d:6.2f}s, "
          f"processed {eng._processed})", flush=True)
    return rate


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    symbol = sys.argv[2] if len(sys.argv) > 2 else "ES"
    events = dt.synth_trades(n, symbol)

    st = _roster(symbol)
    print(f"{len(st)} sleeves, {n} events ({symbol}), through the REAL "
          f"LiveEngine loop\n")

    flat = _run("loop, FLAT (the healthy hours)", _roster(symbol), events, symbol)

    st2 = _roster(symbol)
    eng = _engine(st2, events, symbol)
    held = _seed_positions(eng, st2, symbol)
    t0 = time.perf_counter()
    asyncio.run(eng.run())
    d = time.perf_counter() - t0
    holding = n / d
    print(f"{'loop, HOLDING (' + str(held) + ' resting)':34s} {holding:9.1f} ev/s"
          f"   ({d:6.2f}s, processed {eng._processed})")

    print(f"\nholding costs {flat / holding:.2f}x" if holding else "")
    print(f"live demand is ES ~40/s + NQ ~150/s; the engine was observed "
          f"draining 0.26-2.1 ev/s on 2026-08-17")

    # who is slow, inside the loop
    st3 = _roster(symbol)
    eng3 = _engine(st3, events[: min(n, 4000)], symbol)
    _seed_positions(eng3, st3, symbol)
    pr = cProfile.Profile()
    pr.enable()
    asyncio.run(eng3.run())
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(16)
    print("\n".join(s.getvalue().splitlines()[4:26]))


if __name__ == "__main__":
    main()
