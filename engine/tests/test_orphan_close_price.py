"""An orphan from a FINISHED session is booked out at that session's close.

The engine flattens an unrestorable position at the current mark. That is right
when the restart is inside the same session. It is wrong across a gap.

2026-08-05 is the case: the engine died at 11:10 ET on 08-04 holding 21 intraday
positions, and was restarted at 04:31 ET the next morning. Those sleeves are all
`holds_overnight = False` — the engine's own 15:59 session flat would have closed
every one of them that afternoon. Marking them at the next morning's price books
them for a whole overnight session they were never, by design, exposed to.

So the runner — which reads claude_paper_fills and therefore knows each
position's last fill time — supplies the close price when that fill's ET session
has already ended. The engine uses it verbatim; None means "same session, use
the mark".
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.run_live import (load_open_paper_positions,  # noqa: E402
                            session_close_px)

NS = 1_000_000_000


class _QDB:
    """Serves claude_paper_fills and claude_bars_live from in-memory frames."""

    def __init__(self, fills=None, bars=None):
        self._fills = fills if fills is not None else pd.DataFrame(
            columns=["ts", "sleeve", "side", "qty", "price"])
        self._bars = bars if bars is not None else pd.DataFrame(
            columns=["ts", "c"])

    def df(self, sql):
        if "claude_paper_fills" in sql:
            return self._fills
        if "claude_bars_live" in sql:
            return self._bars
        raise AssertionError(f"unexpected query: {sql}")


def _fills(rows):
    return pd.DataFrame(rows, columns=["ts", "sleeve", "side", "qty", "price"])


def _ts(s):
    return pd.Timestamp(s, tz="UTC").value


# ── the position rebuild now carries WHEN ────────────────────────────────────

def test_open_position_carries_the_time_of_its_last_fill():
    """Without it the runner cannot tell a same-session restart from a
    cross-session one, and every orphan gets marked at today's price."""
    q = _QDB(_fills([(_ts("2026-08-04T13:20:00"), "ES:opendrive", 1, 2, 29003.56)]))
    out = load_open_paper_positions(q, ["ES"])
    assert "ES:opendrive" in out
    pos, avg, last_ts = out["ES:opendrive"]
    assert (pos, avg) == (2, 29003.56)
    assert last_ts == _ts("2026-08-04T13:20:00"), "last fill time not carried"


def test_closed_round_trips_still_drop_out():
    q = _QDB(_fills([(_ts("2026-08-04T13:20:00"), "ES:vwapbreak", 1, 1, 7000.0),
                     (_ts("2026-08-04T14:20:00"), "ES:vwapbreak", -1, 1, 7010.0)]))
    assert load_open_paper_positions(q, ["ES"]) == {}


# ── choosing the close price ─────────────────────────────────────────────────

def test_finished_session_uses_that_sessions_close():
    """08-04 13:20 ET is a session that has since ended; the fair mark is that
    day's 15:59 ET close, not anything from the next morning."""
    bars = pd.DataFrame({"ts": [_ts("2026-08-04T19:59:00")], "c": [28850.25]})
    q = _QDB(bars=bars)
    px = session_close_px(q, "ES", _ts("2026-08-04T13:20:00"),
                          now_ns=_ts("2026-08-05T10:31:00"))
    assert px == 28850.25, f"got {px}; expected the 08-04 session close"


def test_same_session_returns_none_so_the_engine_uses_the_live_mark():
    q = _QDB(bars=pd.DataFrame({"ts": [_ts("2026-08-05T17:00:00")], "c": [7100.0]}))
    px = session_close_px(q, "ES", _ts("2026-08-05T14:00:00"),
                          now_ns=_ts("2026-08-05T15:30:00"))
    assert px is None, f"same-session restart supplied a close price ({px})"


def test_no_bar_for_that_session_returns_none_rather_than_a_wrong_price():
    """The recorder was dead on 2026-08-04, so this case is real. No price beats
    a price from the wrong day -- the engine then books flat at the entry."""
    px = session_close_px(_QDB(), "ES", _ts("2026-08-04T13:20:00"),
                          now_ns=_ts("2026-08-05T10:31:00"))
    assert px is None


def test_a_database_failure_never_takes_the_startup_down():
    class _Boom:
        def df(self, sql):
            raise RuntimeError("questdb down")

    assert session_close_px(_Boom(), "ES", _ts("2026-08-04T13:20:00"),
                            now_ns=_ts("2026-08-05T10:31:00")) is None
