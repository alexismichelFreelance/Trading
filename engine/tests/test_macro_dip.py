"""MacroDipStrategy: the gate must be a switch, and must FAIL SAFE."""
import datetime as dt
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from engine.core.events import Bar
from engine.strategies.macro_dip import MacroDipStrategy, PCTL_WIN, LOOKBACK


def _ts(day: str, minute: int) -> int:
    y, m, d = map(int, day.split("-"))
    t = dt.datetime(y, m, d, minute // 60, minute % 60,
                    tzinfo=ZoneInfo("America/New_York"))
    return int(t.timestamp() * 1_000_000_000)


def _seed(n=PCTL_WIN + LOOKBACK + 5, start=100.0):
    """A gently rising series, so a sharp 3-day drop is a genuine tail."""
    d = date(2024, 1, 1)
    return [(str(d + timedelta(days=i)), start + i * 0.05) for i in range(n)]


def _gate(on: bool, asof: str):
    return {"asof": asof, "gate": {asof: on}}


def _feed(s, day, closes):
    """Drive one session; the last bar is the 15:59 decision bar."""
    out = []
    for i, c in enumerate(closes):
        minute = 15 * 60 + 59 if i == len(closes) - 1 else 10 * 60 + i
        out += s.on_bar(Bar(_ts(day, minute), "1m", c, c, c, c, 100, "ES"))
    return out


def test_dip_with_gate_on_buys():
    day = "2024-10-01"
    s = MacroDipStrategy("ES", seed=_seed(), gate=_gate(True, day),
                         require_gate=True)
    o = _feed(s, day, [100.0, 90.0])
    assert [x.tag for x in o] == ["macrodip-entry"]
    assert o[0].side == 1


def test_same_dip_with_gate_off_does_not_buy():
    day = "2024-10-01"
    s = MacroDipStrategy("ES", seed=_seed(), gate=_gate(False, day),
                         require_gate=True)
    assert _feed(s, day, [100.0, 90.0]) == []


def test_no_dip_does_not_buy_even_with_gate_on():
    day = "2024-10-01"
    s = MacroDipStrategy("ES", seed=_seed(), gate=_gate(True, day),
                         require_gate=True)
    assert _feed(s, day, [100.0, 113.0]) == []


def test_stale_gate_stands_down():
    """A gate older than MAX_STALE_DAYS is neither 'on' nor 'off'."""
    s = MacroDipStrategy("ES", seed=_seed(), gate=_gate(True, "2024-01-15"),
                         require_gate=True)
    assert s.gate_on("2024-10-01") is None
    assert _feed(s, "2024-10-01", [100.0, 90.0]) == []


def test_missing_gate_stands_down(tmp_path):
    s = MacroDipStrategy("ES", seed=_seed(), gate_path=tmp_path / "nope.json",
                         require_gate=True)
    assert s.gate_on("2024-10-01") is None
    assert _feed(s, "2024-10-01", [100.0, 90.0]) == []


def test_default_is_ungated_because_the_gate_did_not_survive():
    day = "2024-10-01"
    s = MacroDipStrategy("ES", seed=_seed(), gate=_gate(False, day))
    assert s.require_gate is False
    assert [x.tag for x in _feed(s, day, [100.0, 90.0])] == ["macrodip-entry"]


def test_cold_start_does_not_trade():
    day = "2024-10-01"
    s = MacroDipStrategy("ES", seed=[], gate=_gate(True, day))
    assert _feed(s, day, [100.0, 90.0]) == []


def test_holds_overnight_is_declared():
    assert MacroDipStrategy("ES").holds_overnight is True


def test_exits_after_the_hold():
    days = [f"2024-10-{i:02d}" for i in range(1, 21)]
    s = MacroDipStrategy("ES", seed=_seed(),
                         gate={"asof": days[-1], "gate": {d: True for d in days}},
                         hold_days=5)
    assert [x.tag for x in _feed(s, days[0], [100.0, 90.0])] == ["macrodip-entry"]

    class _P:
        qty = 1
    s.on_position(_P())
    out = []
    for d in days[1:10]:
        out += _feed(s, d, [95.0, 95.0])
    assert "macrodip-exit" in [x.tag for x in out]
