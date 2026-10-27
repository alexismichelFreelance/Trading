"""Regression: wall-clock EOD backstop must spare holds_overnight sleeves.

The engine's own session flatten (LiveEngine._flatten_for_session) spares EVERY
sleeve with holds_overnight=True. The wall-clock backstop
(RiskSupervisor.trigger_eod_flatten) used to spare ONLY cfg.swing_sleeves
(\"IBSSwingStrategy\",) — and, being new code, now covers PAPER books that the
event-driven on_market path never touched. Production's default paper roster
carries dip3=MacroDipStrategy and rsi2=RSI2SwingStrategy, both holds_overnight=True
and neither in swing_sleeves — so on any live day the backstop flattened them to
zero, defeating their overnight hold even on a healthy feed.

These tests prove the backstop spares the same set _flatten_for_session spares:
a holds_overnight=True paper sleeve that is NOT in swing_sleeves keeps its
position at EOD, while a genuinely-intraday sleeve is still flattened.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.blotter import Blotter                  # noqa: E402
from engine.core.events import Trade                     # noqa: E402
from engine.core.live_engine import LiveEngine           # noqa: E402
from engine.core.orders import Order                     # noqa: E402
from engine.core.risk import RiskConfig, RiskSupervisor  # noqa: E402
from tests.test_eod_backstop import (                    # noqa: E402
    FakeWall, HoldOnce, NullBroker, StallFeed,
)
from tests.test_session_boundary import _ns_at_et        # noqa: E402


class HoldsOvernightSleeve:
    """A sleeve that holds overnight by design — the Engine contract is the
    holds_overnight attribute (used by _flatten_for_session). Production's
    dip3 / rsi2 / ibs all declare it. dip3 & rsi2 are NOT in swing_sleeves."""
    symbol = "ES"
    holds_overnight = True

    def __init__(self):
        self.n = 0

    def on_trade(self, t):
        self.n += 1
        return [Order("ES", 1, 3, tag="entry")] if self.n == 1 else []

    def on_bar(self, b):
        return []

    def on_fill(self, f):
        return None

    def on_position(self, p):
        return None


async def _run(eng, fake, after_eod_ns, n_processed=1):
    eng._eod_backstop_s = 0.02
    task = asyncio.create_task(eng.run())
    loop = asyncio.get_running_loop()
    await eng.wait_processed(n_processed)
    await asyncio.sleep(0.03)
    fake.t = after_eod_ns                             # cross EOD, feed stalls
    deadline = loop.time() + 5.0
    while loop.time() < deadline:
        await asyncio.sleep(0.01)
    eng.stop()
    await task
    return eng


def test_backstop_spares_holds_overnight_but_flattens_intraday():
    """A holds_overnight=True paper sleeve (not in swing_sleeves) must be
    spared by the backstop, while an intraday sleeve is still flattened."""
    morning = _ns_at_et(2026, 10, 2, 10, 0)

    async def go():
        fake = FakeWall(morning)
        # production-shaped config: swing_sleeves names only IBS
        cfg = RiskConfig(eod_flatten_et=(15, 58), swing_sleeves=("IBSSwingStrategy",))
        hold = HoldsOvernightSleeve()
        intraday = HoldOnce()                          # genuinely intraday
        eng = LiveEngine(
            StallFeed([Trade(morning, 7500.0, 1, 1, symbol="ES")]),
            NullBroker(), [hold, intraday], fake, Blotter("ES", 50.0),
            warmup_gate=True, live_owners=set(),        # paper sleeves
            risk=RiskSupervisor(cfg))
        await _run(eng, fake, _ns_at_et(2026, 10, 2, 16, 0), n_processed=1)
        assert eng.risk._eod_done, "supervisor never marked EOD done"
        return (eng.strategy_position(hold),
                eng.strategy_position(intraday),
                eng._eod_backstop_fired)

    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        pos_hold, pos_intraday, fired = asyncio.run(go())
        print(f"hold_pos={pos_hold} intraday_pos={pos_intraday} "
              f"backstop_fired={fired}")

    # The overnight-holding paper sleeve is spared — position survives intact.
    assert pos_hold == 3, (f"holds_overnight paper sleeve was FLATTENED to "
                           f"{pos_hold} by the backstop — contradicts "
                           "_flatten_for_session's holds_overnight contract")
    # The genuinely-intraday sleeve is still flattened.
    assert pos_intraday == 0, ("intraday sleeve was NOT flattened by the "
                               "backstop at EOD")
    assert fired >= 1, "backstop never emitted a flatten"


def test_trigger_eod_flatten_spares_holds_overnight_book():
    """Direct RiskSupervisor check: the 5th book element (holds_overnight)
    exempts a sleeve that is NOT in swing_sleeves."""
    from engine.core.risk import RiskSupervisor as RS
    from tests.test_risk import ts_et
    r = RS(RiskConfig(eod_flatten_et=(15, 58), swing_sleeves=("IBSSwingStrategy",)))
    books = [
        (1, "MacroDipStrategy",  "ES", 3, True),   # holds overnight, not swing
        (2, "ZoneLifecycleStrategy", "ES", 5, False),  # intraday
    ]
    out = r.trigger_eod_flatten(ts_et("15:58"), books)
    ids = [sid for sid, _ in out]
    assert 2 in ids and 1 not in ids, f"holds_overnight book flattened: {ids}"
    assert out[0][1].reduce_only and out[0][1].tag == "risk_eod"


if __name__ == "__main__":
    test_backstop_spares_holds_overnight_but_flattens_intraday()
    test_trigger_eod_flatten_spares_holds_overnight_book()
    print("PASS: holds_overnight sleeves spared by the EOD backstop")
