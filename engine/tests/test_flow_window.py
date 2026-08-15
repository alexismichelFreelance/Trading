"""Flow's LIVE window must close inside the session (16:00 ET), not at the
17:00 ET session edge where the flatten cannot fire until the feed resumes after
the CME maintenance halt. The CLASS default stays (13,21) because flow_oracle.py
is parity-checked against it."""
import sys

import pandas as pd

sys.argv = ["x"]
from engine.strategies.flow import FlowFollowingStrategy   # noqa: E402
from tools.run_live import FLOW_GATE, _make                # noqa: E402


def _et(h_utc):
    return pd.Timestamp(f"2026-07-27 {h_utc:02d}:00", tz="UTC").tz_convert(
        "America/New_York").strftime("%H:%M")


def test_live_flow_closes_at_1600_et_not_1700():
    assert FLOW_GATE == (13, 20)
    assert _et(FLOW_GATE[0]) == "09:00"
    assert _et(FLOW_GATE[1]) == "16:00"      # inside the session, not its edge
    # flow_gex was removed 2026-08-15 with the rest of the _gex family (it
    # gated on a percentile that described the regime at price 28% of the time);
    # flow_lg replaces it and must inherit the same window.
    for lb in ("flow", "flow_lg"):
        assert _make(lb, symbol="ES").gate_utc == FLOW_GATE


def test_parity_default_is_untouched():
    """flow_oracle.py is the parity gate; the class default must not move."""
    assert FlowFollowingStrategy("ES").gate_utc == (13, 21)


def test_flatten_fires_on_an_event_that_actually_arrives():
    """The 2026-07-27 failure mode: the flatten hour must be one where events
    still flow. 20:00 UTC (16:00 ET) is mid-session; 21:00 UTC was the close."""
    lo, hi = FLOW_GATE
    assert lo <= 20 and hi <= 20, "window must close at or before 16:00 ET"
    inside = lo <= 19 < hi          # 15:00 ET still trading
    assert inside
    outside = not (lo <= 20 < hi)   # 16:00 ET -> flatten
    assert outside
