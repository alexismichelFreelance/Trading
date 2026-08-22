"""The gate needs ONE number, and asking for it must not cost 560 microseconds.

READ THIS FIRST: this file does NOT fix the 2026-08-17 lag. It was written
believing it did, and the belief was wrong.

The story it was born from: the engine fell 245 minutes behind with 47,991
events queued and stopped processing exits. GammaCurve.at() was measured at
1,787 calls/s (560 us, 235-strike curve), six curve-gated sleeves were assumed
to ask it ~1,100 times a second, and the arithmetic looked damning.

Then the calls were COUNTED instead of assumed
(five thousand bars and forty thousand trades through the real roster):

    5104 bars      sign_at=0   dist_to_edge=4   at=1560
    40000 trades   sign_at=0   dist_to_edge=0   at=0

Zero on the trade path -- the only path that runs at hundreds of events a
second. Every gate sits behind an entry condition, and the sole per-event caller
of at() is wall_fade, once per BAR, which live is once a minute. 560 us of that
is nothing. The curve did not cause the backlog; the real cause is process-wide
CPU saturation at the US cash open, still open at the time of writing.

What survives is narrower and still true: at() rebuilds four list copies and a
fourteen-key dict and LINEAR-SCANS the whole curve, while gamma_entry_ok wants
one sign and pocket_entry_ok wants one distance. The scalar paths are 27x
cheaper and provably equal (the first two tests). Worth keeping so the gate
stays cheap if entry rates ever rise -- but it is a tidy-up, not a fix.

Two procedural lessons, and the second is the one that cost real money:
tools/dispatch_throughput.py existed, written for exactly this, and nothing
re-measured after the gates were added. And a rate measured without a CALL COUNT
is not a diagnosis -- it is half of one, and the missing half is the half that
says whether the rate matters at all.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.features.gamma_curve import GammaCurve  # noqa: E402

BASIS = 20.0
# a realistically sized book: 235 strikes is what SPX actually carries
ROWS = [(7000.0 + i * 5.0, 10.0 - abs(i - 120) * 0.05, -(10.0 - abs(i - 110) * 0.05))
        for i in range(235)]
CURVE = GammaCurve.from_rows(ROWS, spot=7600.0, basis=BASIS, underlying="SPX")


def test_the_fast_sign_agrees_with_the_rich_lookup():
    """Two paths, one answer. A faster gate that disagrees is not a fix."""
    for px in range(7100, 8100, 7):
        p = float(px) + BASIS
        assert CURVE.sign_at(p) == CURVE.at(p)["local_sign"], f"disagree at {p}"


def test_the_fast_distance_agrees_too():
    for px in range(7100, 8100, 13):
        p = float(px) + BASIS
        a, b = CURVE.dist_to_edge(p), CURVE.at(p)["dist_to_flip"]
        if a is None or b is None:
            assert a is None and b is None, f"disagree at {p}: {a} vs {b}"
        else:
            assert abs(a - b) < 1e-9, f"disagree at {p}: {a} vs {b}"


def test_the_gate_path_is_far_cheaper_than_the_rich_lookup():
    """THE REGRESSION, as a RATIO so it does not flake on a loaded machine.

    An absolute floor is tempting and wrong: measured on this box under load the
    same call ran at 39,884/s and 87,285/s minutes apart. What cannot change is
    that the gate must not pay for four list copies and a fourteen-key dict.

    Calibrated run: empty loop 1,830,858/s, bare bisect 236,735/s,
    sign_at 87,285/s (11.5us), at() 3,262/s (307us) -- 27x. Against a live
    demand of ~1,100 calls/s that is ample; at()'s ceiling was not."""
    n, m = 20000, 2000
    t0 = time.perf_counter()
    for i in range(n):
        CURVE.sign_at(7600.0 + (i % 40) * 0.25 + BASIS)
    fast = (time.perf_counter() - t0) / n
    t0 = time.perf_counter()
    for i in range(m):
        CURVE.at(7600.0 + (i % 40) * 0.25 + BASIS)
    rich = (time.perf_counter() - t0) / m
    assert rich / fast > 8.0, (
        f"sign_at is only {rich / fast:.1f}x cheaper than at(); the gate is "
        f"back on the allocating path that put the engine 245 minutes behind")
    assert fast < 1e-4, f"sign_at costs {fast * 1e6:.0f}us -- far above a bisect"


def test_an_empty_curve_is_still_cheap_and_answers_none():
    c = GammaCurve.from_rows([], spot=0.0, basis=BASIS, underlying="SPX")
    assert c.sign_at(7800.0) == 0
    assert c.dist_to_edge(7800.0) is None
