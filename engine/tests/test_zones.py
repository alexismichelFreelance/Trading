import pandas as pd
import pytest

from engine.adapters.questdb import QuestDB
from engine.core.events import Bar
from engine.features.bars import BarAggregator
from engine.features.pivots import daily_pivots, level_set, nearest_beyond
from engine.features.zones import DEMAND, SUPPLY, Zone, ZoneBook, ZoneDetector

NS = 1_000_000_000
T0 = pd.Timestamp("2025-04-01T13:00:00Z").value


def _db_up() -> bool:
    try:
        QuestDB(timeout=5).query("SELECT 1")
        return True
    except Exception:
        return False


# ── bar aggregation ──────────────────────────────────────────────────────
def test_bar_aggregator_boundaries_and_ohlc():
    agg = BarAggregator(("30m", "1h"))
    out = []
    for m in range(90):                       # minutes 0..89, CLOSE-stamped
        o = 5000.0 + m
        out += agg.update(Bar(T0 + (m + 1) * 60 * NS, "1m", o, o + 0.5, o - 0.5, o + 0.25, 10))
    thirty = [b for b in out if b.tf == "30m"]
    hour = [b for b in out if b.tf == "1h"]
    assert len(thirty) == 2 and len(hour) == 1     # 14:00 buckets still open
    b13 = thirty[0]
    assert b13.o == 5000.0 and b13.v == 300        # 30 x vol 10
    assert b13.h == 5029.5 and b13.c == 5029.25
    assert b13.ts == T0 + 1800 * NS                # close of the 13:00 bucket
    flushed = agg.flush()
    assert len([b for b in flushed if b.tf == "30m"]) == 1   # the open 14:00 bar


# ── pivots ───────────────────────────────────────────────────────────────
def test_daily_pivots_formula():
    p = daily_pivots(110, 90, 100)
    assert p["PP"] == 100 and p["R1"] == 110 and p["S1"] == 90
    assert p["R2"] == 120 and p["S2"] == 80 and p["PDH"] == 110 and p["PDL"] == 90


def test_nearest_beyond_respects_min_dist():
    lv = [90.0, 100.0, 110.0, 120.0]
    assert nearest_beyond(lv, 101, 1, 2) == 110
    assert nearest_beyond(lv, 103, -1, 2) == 100     # 100 is 3pt below -> qualifies
    assert nearest_beyond(lv, 101, -1, 2) == 90      # 100 is <2pt below -> skipped
    assert nearest_beyond(lv, 109, 1, 2) == 120      # 110 is <2pt away -> skipped


def test_level_set_has_rounds_and_pivots():
    lv = level_set(5120, 5080, 5100, round_step=50.0)
    assert 5100.0 in lv      # PP
    assert 5100.0 in lv and any(abs(x % 50) < 1e-9 for x in lv)


# ── zone detection ───────────────────────────────────────────────────────
def _bar(i, o, h, l, c, v):
    return Bar(T0 + i * 1800 * NS, "30m", o, h, l, c, v)


def test_detect_demand_zone():
    det = ZoneDetector()
    z = None
    for i in range(20):                                   # warmup range 2, vol 100
        z = det.update(_bar(i, 5000, 5001, 4999, 5000, 100)) or z
    for i in (20, 21):                                    # tight base range 0.6, vol 80
        z = det.update(_bar(i, 5000, 5000.3, 4999.7, 5000, 80)) or z
    z = det.update(_bar(22, 5000.0, 5005.0, 5000.0, 5004.0, 200)) or z   # up departure
    assert z is not None and z.direction == DEMAND
    assert abs(z.top - 5000.3) < 1e-9 and abs(z.bot - 4999.7) < 1e-9
    assert z.departure_score == 2 and z.base_score == 2


def test_zone_lifecycle_touch_break_flip():
    book = ZoneBook()
    z = Zone(0, 5000.3, 4999.7, DEMAND, 2, 2)
    book.add(z)
    assert z.virgin
    book.on_price(5000.0, 1)
    assert z.touches == 1 and not z.virgin
    book.on_price(5010.0, 2)
    book.on_price(5000.0, 3)
    assert z.touches == 2
    flips = book.on_bar(Bar(4, "30m", 5000, 5001, 4998, 4998, 100))   # closes below bot
    assert z.broken and len(flips) == 1 and flips[0].direction == SUPPLY


def test_nearest_opposing_virgin_targets():
    book = ZoneBook()
    sup = Zone(0, 5025, 5020, SUPPLY, 2, 1)
    dem = Zone(0, 4980, 4975, DEMAND, 2, 1)
    book.add(sup)
    book.add(dem)
    assert book.nearest_opposing(5000, 1) is sup     # long -> supply above
    assert book.nearest_opposing(5000, 1).proximal() == 5020
    assert book.nearest_opposing(5000, -1) is dem     # short -> demand below
    assert book.nearest_opposing(5000, -1).proximal() == 4980


@pytest.mark.skipif(not _db_up(), reason="QuestDB not reachable")
def test_zone_frequency_matches_research():
    q = QuestDB()
    df = q.df("SELECT ts, first(o) o, max(h) h, min(l) l, last(c) c, sum(vol) v "
              "FROM claude_bars_1m WHERE symbol='ESM5' SAMPLE BY 30m ALIGN TO CALENDAR")
    df = df.dropna(subset=["c"])
    det = ZoneDetector()
    n = 0
    for r in df.itertuples():
        if det.update(Bar(int(r.ts.value), "30m", r.o, r.h, r.l, r.c, int(r.v))):
            n += 1
    days = df["ts"].dt.tz_convert("America/New_York").dt.date.nunique()
    rate = n / days
    print(f"\nESM5 zones: {n} over {days} days = {rate:.2f}/day (research ~0.51)")
    assert n > 0 and 0.2 < rate < 1.5
