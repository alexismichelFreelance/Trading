"""How many bars per second can the engine actually dispatch?

2026-08-12: after a 09:45 ET restart the engine did not go LIVE until 10:48:55
wall clock, and the trade it flipped on was stamped 09:39:39 -- it was deciding
on data 69 minutes old. Over the same window the raw-capture tee (a tee on the
same socket, its own thread) was 2-7 minutes behind, so the delay was not the
socket: it was between the feed pump and the dispatch loop. LiveEngine._q is an
unbounded asyncio.Queue with no depth instrumentation, so a dispatch slower than
the tape is invisible -- it looks exactly like a healthy engine.

NT8 sends ~5100 backfill bars on connect. The engine logged
"5104 backfill bars consumed" at the flip, 59 minutes after the feed connected:
1.4 bars/s. This measures that number directly, with no NT8 and no database:
build the real roster, push N bars through the real dispatch path, and time it.

    .venv/Scripts/python.exe tools/dispatch_throughput.py [n_bars] [symbol]

Prints bars/s and the per-strategy cost, so the hot sleeve is named rather than
guessed at.
"""
from __future__ import annotations

import cProfile
import io
import pstats
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.dispatch import dispatch_market   # noqa: E402
from engine.core.events import Bar                 # noqa: E402
from engine.features.gamma_curve import GammaCurve  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
import run_live                                    # noqa: E402

NS = 1_000_000_000
# 2026-08-11 09:30 ET, so the bars land inside RTH and the sleeves actually work
T0 = 1_786_534_200 * NS


def synth_bars(n: int, symbol: str) -> list[Bar]:
    """A plausible tape: a slow drift with 1-3 point bars. Shape barely matters
    for throughput -- what matters is that every feature and detector runs."""
    out, px = [], 7700.0
    for i in range(n):
        px += ((i * 7919) % 17 - 8) * 0.25
        o = px
        h = px + ((i * 104729) % 5) * 0.25
        l = px - ((i * 15485863) % 5) * 0.25
        c = px + ((i * 32452843) % 9 - 4) * 0.25
        out.append(Bar(T0 + i * 60 * NS, "1m", o, max(o, h, c), min(o, l, c),
                       c, 1000 + (i % 500), symbol))
    return out


def synth_trades(n: int, symbol: str) -> list:
    from engine.core.events import BUY, SELL, Trade
    out, px = [], 7700.0
    for i in range(n):
        px += ((i * 7919) % 5 - 2) * 0.25
        out.append(Trade(T0 + i * 100_000_000, px, 1 + (i % 4),
                         BUY if i % 2 else SELL, symbol))
    return out


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5104
    symbol = sys.argv[2] if len(sys.argv) > 2 else "ES"
    roster = run_live.build_roster("all", symbol=symbol)
    strategies = [s for _, s in roster] if isinstance(roster[0], tuple) else list(roster)

    # ATTACH THE CURVES. Without this the tool measures a roster whose gates all
    # return True on the first line (pocket is None), which is why it did not
    # catch the 2026-08-17 regression: the gate cost lives entirely in the curve
    # lookup. Injected, not loaded -- no DB, and the size is what matters.
    rows = [(7000.0 + i * 5.0, 10.0 - abs(i - 120) * 0.05,
             -(10.0 - abs(i - 110) * 0.05)) for i in range(235)]
    curve = GammaCurve.from_rows(rows, spot=7600.0, basis=20.0, underlying="SPX")
    run_live.attach_curves(strategies, symbol, "2026-08-11", curve=curve)
    gated = sum(1 for s in strategies if getattr(s, "pocket", None) is not None)

    bars = synth_bars(n, symbol)
    print(f"{len(strategies)} sleeves ({gated} curve-gated, {len(rows)} strikes), "
          f"{n} bars ({symbol})")

    t0 = time.perf_counter()
    for b in bars:
        dispatch_market(strategies, b)
    dt = time.perf_counter() - t0
    rate = n / dt
    print(f"\nBARS   {dt:7.1f}s   {rate:8.1f} bars/s"
          f"   -> NT8's ~5100-bar backfill costs {5104 / rate:.0f}s")

    # TRADES. The backfill is bars, but a RECONNECT replays the session's TICK
    # history, and every one of those goes through the same dispatch. 2026-08-12
    # recorded 678k NQ rows in the hour the engine spent catching up.
    trades = synth_trades(min(n * 20, 40000), symbol)
    t0 = time.perf_counter()
    for tr in trades:
        dispatch_market(strategies, tr)
    dt = time.perf_counter() - t0
    trate = len(trades) / dt
    print(f"TRADES {dt:7.1f}s   {trate:8.0f} trades/s")
    # what one instrument really prints, measured off the tape we recorded
    print(f"\nES prints ~40/s and NQ ~150/s in RTH, so one lane's live tape is "
          f"well inside {trate:.0f}/s -- but a reconnect asks the engine to "
          f"REPLAY every tick since the session open on top of the live flow. "
          f"An hour of NQ at 150/s is ~540k events: {540000 / trate / 60:.0f} "
          f"minutes at this rate, during which the engine is behind by exactly "
          f"that much and its own staleness check cannot see it (it compares "
          f"each event to the newest it has SEEN, and a uniformly delayed "
          f"stream ascends perfectly).")

    # who is slow?
    pr = cProfile.Profile()
    pr.enable()
    for b in bars[: min(n, 500)]:
        dispatch_market(strategies, b)
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(18)
    print("\n".join(s.getvalue().splitlines()[4:28]))


if __name__ == "__main__":
    main()
