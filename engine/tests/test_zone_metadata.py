"""Zone entries must record WHICH zone they took.

engine/features/zones.py already scores every zone -- departure_score (0-2) +
base_score (0-2), a composite 0-4 -- and tracks `touches` and `virgin`. The
detector uses them as a gate and then throws them away: _ZoneRec never copied
them, and the fill tag was a bare "fade-entry".

So the trade record cannot answer the only question that matters about a zone
sleeve: did the strong, untouched zones do better than the weak, re-tested ones?
Across 36 ES sessions the answer is not "no", it is UNKNOWABLE, and no amount of
replaying fixes that because the information was never written down.

What the record already shows without it, and what makes recording it urgent:

    ES:zones  by setup      flip  6 trips  +7,288  100% win
                            fade  7 trips  +6,412   86% win
                            break 4 trips  -1,938   25% win

Break is a losing setup and fade/flip are not. If strength separates outcomes
the same way, the sleeve can be gated on it -- but only if it is stamped at
entry, on the fill, at the moment the decision is made.

The tag keeps its setup prefix so every existing consumer that reads
"fade-entry" keeps working; the metadata is appended.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.strategies.zones_strategy import parse_zone_tag  # noqa: E402


def test_tag_round_trips_every_field():
    """THE PRIMITIVE: what is written must be readable back, exactly."""
    from engine.strategies.zones_strategy import zone_tag
    t = zone_tag("fade", dep=2, base=1, touches=0, tf="30m")
    m = parse_zone_tag(t)
    assert m["setup"] == "fade"
    assert m["strength"] == 3          # dep + base, the composite the detector uses
    assert m["dep"] == 2 and m["base"] == 1
    assert m["touches"] == 0
    assert m["virgin"] is True         # touches == 0
    assert m["tf"] == "30m"


def test_setup_prefix_survives_so_existing_readers_keep_working():
    from engine.strategies.zones_strategy import zone_tag
    for s in ("fade", "break", "flip"):
        t = zone_tag(s, dep=1, base=1, touches=2, tf="1h")
        assert t.startswith(f"{s}-entry"), t


def test_a_retested_zone_is_not_virgin():
    from engine.strategies.zones_strategy import zone_tag
    m = parse_zone_tag(zone_tag("fade", dep=2, base=2, touches=1, tf="30m"))
    assert m["touches"] == 1 and m["virgin"] is False
    assert m["strength"] == 4


def test_plain_tags_parse_to_nothing_rather_than_exploding():
    """The table holds months of un-annotated fills; reading them must not raise."""
    for t in ("session-flat", "target", "be", "scale", "", None):
        assert parse_zone_tag(t) is None


def test_timeframe_is_recorded_so_variants_are_distinguishable():
    """zones_15m / zones_1h / zones_4h all write to the same table; without the
    tf on the fill a confluence study cannot tell which bucket produced which
    trade."""
    from engine.strategies.zones_strategy import zone_tag
    tfs = {parse_zone_tag(zone_tag("flip", dep=1, base=0, touches=0, tf=x))["tf"]
           for x in ("15m", "30m", "1h", "4h")}
    assert tfs == {"15m", "30m", "1h", "4h"}


def test_the_sleeve_stamps_a_real_entry():
    """End to end: the SLEEVE must stamp the tag, not just the helper.

    ZoneDetector needs avg_window=20 closed 30m bars before it will form
    anything, and the sleeve only feeds bars inside 09:00-17:00 ET -- 16 buckets
    a day. So a zone cannot form within one session; this needs prior days of
    history, which is exactly why the naive single-day fixture saw nothing.

    Day 1-2 are filler that build the range average. Day 3 lays a tight BASE,
    then one large DEPARTURE (>= 1.4x avg range, body >= 50% of it, volume at
    least average) which leaves a zone at the base, then returns to it."""
    import pandas as pd
    from engine.core.events import Bar
    from engine.strategies.zones_strategy import ZoneLifecycleStrategy

    s = ZoneLifecycleStrategy("ES", point_usd=50.0)
    out = []

    def feed(day, mins, lo, hi, step, vol=500):
        """`mins` 1-minute bars walking lo->hi from 09:00 ET on `day`."""
        start = int(pd.Timestamp(f"{day} 09:00", tz="America/New_York").value)
        for j in range(mins):
            px = lo + (hi - lo) * (j / max(1, mins - 1))
            t = start + j * 60_000_000_000
            out.extend(s.on_bar(Bar(t, "1m", px, px + step, px - step, px, vol, "ES")) or [])

    # Two filler sessions set the running average range. They must be WIDER
    # than the base that follows: a base bar qualifies at <= 0.8 * avg_range,
    # so a base is only recognisable relative to what came before it.
    feed("2026-08-03", 480, 7000.0, 7030.0, 1.0)
    feed("2026-08-04", 480, 7030.0, 7000.0, 1.0)

    # day 3, built bucket by bucket
    start = int(pd.Timestamp("2026-08-05 09:00", tz="America/New_York").value)
    n = 0

    def bucket(lo, hi, step, vol=500):
        nonlocal n
        for j in range(30):
            px = lo + (hi - lo) * (j / 29.0)
            t = start + n * 60_000_000_000
            n += 1
            out.extend(s.on_bar(Bar(t, "1m", px, px + step, px - step, px, vol, "ES")) or [])

    for _ in range(3):
        bucket(7000.0, 7000.2, 0.2)              # tight base (~0.6 vs ~3.9 avg)
    bucket(7000.0, 6940.0, 1.0, vol=900)         # departure -> supply zone at the base
    bucket(6940.0, 6955.0, 0.5)
    for _ in range(3):
        bucket(6955.0, 7000.0, 0.5)              # return to the zone
    bucket(7000.0, 6985.0, 0.5)

    assert s.zones, "detector formed no zones at all"
    entries = [o.tag for o in out if "-entry" in (o.tag or "")]
    assert entries, (f"{len(s.zones)} zones formed but the sleeve never entered; "
                     f"tags: {sorted({o.tag for o in out})}")
    m = parse_zone_tag(entries[0])
    assert m is not None, f"entry tag carries no zone metadata: {entries[0]!r}"
    assert m["tf"] == "30m"
    assert 0 <= m["strength"] <= 4
    assert m["setup"] in ("fade", "break", "flip")
