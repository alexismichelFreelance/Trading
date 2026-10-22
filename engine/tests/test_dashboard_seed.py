"""The live dashboard's chart lane must be seeded from REAL bars, on the
engine's own grid, without touching the engine's trading state.

THE HOLE THIS EXISTS TO FILL. The NT8 relay replays the CHART's history when
the engine connects, and that history can lag the live edge by DAYS: the
chart then ends days before the live edge with a hole in the middle, and the
VWAP bands the chart draws have a hole in the same place (the engine's book
only ever saw the bars the relay replayed). Our own recorder table holds the
SAME feed's 1m bars, so the dashboard's candles and its bands are built from
there -- real, same-source bars that reach the live edge.

WHAT MUST NOT HAPPEN. The engine's own LevelBook belongs to a strategy: it is
what the strategies trade off. The dashboard gets its OWN book. A seed that
fed the engine's book would move the levels every strategy reads, which is a
trading change dressed up as a chart fix.

THE GRID. The seeded candles are built with the engine's own BarAggregator, so
a seeded 5m candle carries the same timestamp as the engine's own 5m bar.
One bucket stamped a minute off would interleave with the live bars instead
of merging with them, and the chart's time axis would show two candles per
period.

AS OF. The VWAP line and its sigma bands are TIME SERIES: each point is the
book's value AT THAT BAR. Seeding them against the book's FINAL values would
stamp one flat line at today's VWAP across four days of history -- a chart
that looks alive and says nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.events import Bar                                  # noqa: E402
from engine.features.bars import BarAggregator                      # noqa: E402
from engine.features.level_book import LevelBook                   # noqa: E402
from engine.features.state_store import StateStore                 # noqa: E402
from tools.run_live import seed_dashboard_lane                      # noqa: E402

NS = 1_000_000_000
BASE = 1_767_225_600 * NS          # 2026-01-01T00:00:00Z


def _rows(n: int = 30) -> list:
    """n closed 1m bars, one minute apart, on a clean grid."""
    out = []
    for i in range(n):
        ts = BASE + i * 60 * NS
        out.append(Bar(ts, "1m", 7700.0 + i, 7701.0 + i, 7699.0 + i,
                       7700.5 + i, 100 + i, "ES"))
    return out


def _series(store, lane, sid):
    for s in store.snapshot()["lanes"][lane]["indicators"]:
        if s["id"] == sid:
            return s["points"]
    return []


def test_seed_returns_its_own_book():
    """A fresh LevelBook for the dashboard; the engine's book is untouched."""
    rows = _rows()
    store = StateStore()
    lb = seed_dashboard_lane(store, "ES", rows, ("5m",))
    assert isinstance(lb, LevelBook)
    assert lb.vwap.n == len(rows)


def test_dashboard_book_ignores_non_1m_bars():
    """LevelBook.on_bar takes 1m only; a 5m bar must not move the VWAP."""
    rows = _rows(5)
    lb = seed_dashboard_lane(StateStore(), "ES", rows, ("5m",))
    before = lb.vwap.n
    lb.on_bar(Bar(BASE + 600 * NS, "5m", 1.0, 2.0, 0.5, 1.5, 999, "ES"))
    assert lb.vwap.n == before


def test_seeded_candles_land_on_the_engine_grid():
    """The seeded 5m candles must carry the SAME timestamps as the engine's
    own aggregation of the same 1m bars -- not a bucket off."""
    rows = _rows(30)
    store = StateStore()
    seed_dashboard_lane(store, "ES", rows, ("5m",))

    engine_agg = BarAggregator(("5m",), input_tf="1m")
    expected = []
    for b in rows:
        expected.extend(engine_agg.update(b))
    expected.extend(engine_agg.flush())
    exp_ts = sorted(int(b.ts) for b in expected)

    got = sorted(int(b["ts"]) for b in store.snapshot()["lanes"]["ES"]["bars"]["5m"])
    assert got == exp_ts
    assert len(got) > 0


def test_seeded_raw_tape_is_the_1m_rows():
    store = StateStore()
    rows = _rows(10)
    seed_dashboard_lane(store, "ES", rows, ("5m",))
    raw = store.snapshot()["lanes"]["ES"]["bars_raw"]
    assert len(raw) == 10
    assert sorted(int(b["ts"]) for b in raw) == [int(b.ts) for b in rows]
    # the raw tape is 1m, the views are 5m: one grid each, never mixed
    assert set(b["tf"] for b in raw) == {"1m"}


def test_seed_keeps_the_newest_window_by_time():
    """The store's window is bounded by TIME: a long seed keeps the NEWEST
    bars, so the chart still ends at the live edge."""
    rows = _rows(4000)
    store = StateStore()
    seed_dashboard_lane(store, "ES", rows, ("5m",))
    snap = store.snapshot()["lanes"]["ES"]
    raw_ts = sorted(int(b["ts"]) for b in snap["bars_raw"])
    assert len(raw_ts) == 1800                       # MAX_RAW_BARS
    assert raw_ts[-1] == int(rows[-1].ts)            # the newest bar survives
    agg_ts = sorted(int(b["ts"]) for b in snap["bars"]["5m"])
    assert len(agg_ts) == 600                        # MAX_AGG_BARS
    # the view is stamped at its BUCKET CLOSE, so the last candle closes on
    # the next 5m boundary after the last 1m bar
    assert 0 <= agg_ts[-1] - int(rows[-1].ts) <= 300 * NS


def test_seeded_vwap_is_a_real_time_series():
    """Each seeded point is the book's value AT THAT BAR, not the final one.

    A flat line at today's VWAP across the whole seeded range is the failure
    this catches: the chart would render, look plausible, and carry no
    information about where price actually traded relative to its average.
    """
    rows = _rows(60)
    store = StateStore()
    seed_dashboard_lane(store, "ES", rows, ("5m",))
    pts = _series(store, "ES", "vwap")
    assert len(pts) == len(rows)
    assert pts[0]["ts"] == int(rows[0].ts)
    assert pts[-1]["ts"] == int(rows[-1].ts)
    # the FIRST point is the first bar's close -- only the close is known
    # there, which is exactly what an as-of series means
    assert pts[0]["v"] == rows[0].c
    # and the series MOVES: it is not one value stamped over and over
    assert len({p["v"] for p in pts}) > 1
    assert pts[-1]["v"] != pts[0]["v"]


def test_seeded_bands_accompany_the_line():
    """The sigma bands are part of the same series family -- the seeded range
    carries them too, or the chart draws a bare line with no band."""
    rows = _rows(120)
    store = StateStore()
    seed_dashboard_lane(store, "ES", rows, ("5m",))
    for sid in ("vwap_2s_hi", "vwap_2s_lo"):
        pts = _series(store, "ES", sid)
        assert len(pts) > 0
        assert pts[0]["ts"] == int(rows[0].ts)
