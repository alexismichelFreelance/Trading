"""Corrupt bar history must not reach the feature pipeline.

Between 2026-09-29 and 2026-10-02 the recorder wrote 4,140 rows under the
symbol 'ES' carrying NQ prices (30,657 - 31,280 while ES traded 7,700 - 7,900).
Those rows are still in QuestDB, so every loader that reads history has to
defend itself: the zone detector seeds its book from replayed 1m history, and a
zone whose base window overlapped the bad block inherited a bogus edge at
31,159 -- which stretched the dashboard's price scale 4x and squeezed the
candles into an invisible sliver.

The filter anchors on the MEDIAN close of the window, so a minority of bad
rows cannot move the anchor, and it must never touch legitimate structure.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.barsanity import (  # noqa: E402
    DEFAULT_BAND,
    filter_bars,
    filter_frame,
    plausible_window,
)
from engine.core.events import Bar  # noqa: E402


def _bar(ts: int, close: float, o=None, h=None, l=None):
    o = close if o is None else o
    h = close if h is None else h
    l = close if l is None else l
    return Bar(ts, "1m", o, h, l, close, 1000, "ES")


def test_plausible_window_anchors_on_the_median():
    closes = [7800.0] * 90 + [31000.0] * 10
    lo, hi = plausible_window(closes)
    assert lo == 7800.0 * (1.0 - DEFAULT_BAND)
    assert hi == 7800.0 * (1.0 + DEFAULT_BAND)


def test_plausible_window_without_usable_closes_is_none():
    assert plausible_window([]) is None
    assert plausible_window([0.0, -1.0, None]) is None


def test_filter_bars_drops_the_nq_price_block():
    good = [_bar(i * 60_000_000_000, 7800.0 + (i % 7) * 0.25) for i in range(200)]
    bad = [_bar(i * 60_000_000_000, 31000.0 + (i % 5) * 0.25) for i in range(50)]
    kept, dropped = filter_bars(good + bad)
    assert dropped == 50
    assert len(kept) == 200
    assert all(b.c < 10000 for b in kept)


def test_filter_bars_keeps_legitimate_structure():
    """A wide but real range (ES 7,000 -> 9,000 over a window) stays put."""
    good = [_bar(i * 60_000_000_000, 7000.0 + i * 10.0) for i in range(200)]
    kept, dropped = filter_bars(good)
    assert dropped == 0
    assert len(kept) == 200


def test_filter_bars_drops_a_bar_whose_wick_is_corrupt():
    """The close may look sane while the high carries the wrong instrument."""
    bars = [_bar(i * 60_000_000_000, 7800.0) for i in range(100)]
    bars.append(_bar(100 * 60_000_000_000, 7800.0, h=31232.5))
    kept, dropped = filter_bars(bars)
    assert dropped == 1
    assert len(kept) == 100


def test_filter_bars_is_a_noop_on_empty_input():
    kept, dropped = filter_bars([])
    assert kept == []
    assert dropped == 0


def test_filter_frame_drops_implausible_rows():
    pd = pytest_import_pandas()
    rows = [{"ts": i, "o": 7800.0, "h": 7801.0, "l": 7799.0, "c": 7800.0,
             "vol": 100} for i in range(200)]
    rows += [{"ts": i, "o": 31000.0, "h": 31001.0, "l": 30999.0, "c": 31000.0,
              "vol": 100} for i in range(200, 250)]
    df = pd.DataFrame(rows)
    kept, dropped = filter_frame(df)
    assert dropped == 50
    assert len(kept) == 200
    assert kept["c"].max() < 10000


def test_filter_frame_is_a_noop_on_a_clean_frame():
    pd = pytest_import_pandas()
    df = pd.DataFrame([{"ts": i, "o": 7800.0, "h": 7801.0, "l": 7799.0,
                         "c": 7800.0, "vol": 100} for i in range(50)])
    kept, dropped = filter_frame(df)
    assert dropped == 0
    assert len(kept) == 50


def pytest_import_pandas():
    import pandas
    return pandas
