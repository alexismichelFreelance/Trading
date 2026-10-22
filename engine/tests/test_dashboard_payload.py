"""The WS payload must carry the indicator series on the SAME grid as the
candles.

WHY. The engine's indicator series is stamped at the BAR's resolution (1m)
while the chart draws one candle per 5 minutes. Two things follow from
sending the raw 1m points:

  * the payload is five times bigger for the same picture (10,800 points
    instead of 3,600 for the same six series), and
  * LWC's time scale derives its slot width from the SMALLEST gap between
    consecutive times across ALL series -- so a 1m indicator tail next to
    5m candles squeezes the axis and every candle is painted five times
    too narrow. The client defends itself by snapping on arrival; the
    server doing it first means the frame the browser downloads is already
    the frame the chart draws.

THE STATIC LEVEL SERIES ARE NOT SNAPPED. The pivot grid and the dealer
gamma carry SEVERAL levels at ONE timestamp. Collapsing those onto a single
candle would merge nine levels into one -- the chart would draw one line
instead of the grid.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.run_dashboard import Dashboard, _snap_series_to_grid      # noqa: E402

NS = 1_000_000_000
BASE = 1_767_225_600 * NS


def _candles(n: int = 10, step_s: int = 300):
    """n candles on a 5m grid, closing on the bucket boundary."""
    return [{"ts": BASE + (i + 1) * step_s * NS, "tf": "5m"} for i in range(n)]


def test_series_is_snapped_onto_the_candle_grid():
    """Every point lands ON a candle timestamp, and the newest value in the
    slot wins."""
    grid = [c["ts"] for c in _candles(4)]
    # two points inside the FIRST candle (which closes at +300s), one inside
    # the second (+600s)
    pts = [{"ts": BASE + 150 * NS, "v": 1.0},
           {"ts": BASE + 250 * NS, "v": 2.0},
           {"ts": BASE + 550 * NS, "v": 3.0}]
    out = _snap_series_to_grid({"id": "vwap", "producer": "levelbook:avwap",
                                 "label": "VWAP", "unit": "price",
                                 "points": pts}, grid)
    assert [p["ts"] for p in out["points"]] == [grid[0], grid[1]]
    # the LATEST value in the slot wins (the value as of the bar's close)
    assert out["points"][0]["v"] == 2.0
    assert out["points"][1]["v"] == 3.0
    assert out["id"] == "vwap" and out["producer"] == "levelbook:avwap"


def test_points_with_no_candle_near_them_are_dropped():
    grid = [c["ts"] for c in _candles(2)]
    out = _snap_series_to_grid({"id": "vwap", "producer": "levelbook:avwap",
                                 "points": [{"ts": BASE - 3600 * NS, "v": 1.0}]}, grid)
    assert out["points"] == []


def test_empty_series_and_empty_grid_pass_through():
    s = {"id": "vwap", "producer": "levelbook:avwap", "points": []}
    assert _snap_series_to_grid(s, [])["points"] == []
    assert _snap_series_to_grid(s, [BASE])["points"] == []


def test_lane_payload_puts_every_series_on_the_candles_grid():
    """THE CLIENT'S INVARIANT: one grid across every series, or LWC's slot
    width collapses and the chart is drawn at the wrong scale."""
    candles = _candles(20)
    grid = [c["ts"] for c in candles]
    snap = {"lanes": {"ES": {
        "bars": {"5m": candles},
        "bars_raw": [],
        # 1m indicator points: three inside the first candle, one at the end
        "indicators": [
            {"id": "vwap", "producer": "levelbook:avwap", "unit": "price",
             "points": [{"ts": BASE + 60 * NS, "v": 1.0},
                        {"ts": BASE + 120 * NS, "v": 2.0},
                        {"ts": grid[-1] - 60 * NS, "v": 3.0}]},
            # the STATIC grid: nine levels at ONE timestamp
            {"id": "pivots", "producer": "levelbook:pivots", "unit": "price",
             "points": [{"ts": BASE, "v": 7000.0 + i} for i in range(9)]},
            {"id": "gamma", "producer": "levelbook:gamma_levels", "unit": "price",
             "points": [{"ts": BASE, "v": 7100.0}]},
        ],
    }}}
    payload = Dashboard._lane_payload(snap, "ES", "5m")
    by_id = {i["id"]: i for i in payload["indicators"]}
    assert set(by_id) == {"vwap", "pivots", "gamma"}
    # the time series is on the candle grid
    assert all(p["ts"] in grid for p in by_id["vwap"]["points"])
    # the static series keep their own timestamps AND all their levels
    assert len(by_id["pivots"]["points"]) == 9
    assert {p["ts"] for p in by_id["pivots"]["points"]} == {BASE}
    assert len(by_id["gamma"]["points"]) == 1
