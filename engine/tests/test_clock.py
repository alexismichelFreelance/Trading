import pytest

from engine.core.clock import EventClock, WallClock


def test_event_clock_advances_and_allows_equal():
    c = EventClock()
    c.set(100)
    assert c.now() == 100
    c.set(100)  # repeated ts within a 1s bucket is fine
    assert c.now() == 100
    c.set(200)
    assert c.now() == 200


def test_event_clock_rejects_regression():
    c = EventClock(50)
    with pytest.raises(ValueError):
        c.set(49)


def test_wall_clock_monotone_nonneg():
    w = WallClock()
    a = w.now()
    b = w.now()
    assert b >= a > 0
    w.set(123)  # no-op, must not raise
