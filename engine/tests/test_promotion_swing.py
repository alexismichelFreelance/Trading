"""The promotion gate cannot measure a sleeve that holds overnight.

tools/promotion_check.py walks fills with `groupby(["sleeve", "day"])` and
re-initialises `pos, cost, real = 0, 0.0, 0.0` inside that loop, so a position is
silently abandoned at every session boundary. A sleeve that ENTERS on Monday and
EXITS on Thursday therefore never pairs its fills and realises exactly nothing.

That is not a scoring quirk, it is a structural blindness: G5 (sign consistency),
G6 (mean & median > 0) and G8 (worst vs median) are all computed from this
series, so every swing sleeve reports +0.00pt on all three and is BLOCKED no
matter how it actually performed. IBSSwingStrategy -- the one sleeve in the book
with a 16-year, cost-net, out-of-sample-confirmed record and `holds_overnight =
True` -- can never be promoted while this stands.

Intraday sleeves are unaffected, which is why it went unnoticed: they open and
close inside one session, so the daily reset happens to be harmless for them.
Both cases are pinned here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.promotion_check import paper_pnl  # noqa: E402

# 15:59 ET in July is 19:59 UTC -- the MOC decision bar IBS trades on.
MOC = "T19:59:00.000000Z"


class FakeQDB:
    """Stands in for QuestDB: paper_pnl only ever calls .df(sql)."""

    def __init__(self, rows: list[dict]) -> None:
        self._df = pd.DataFrame(rows)

    def df(self, sql: str) -> pd.DataFrame:
        return self._df.copy()


def _fill(day: str, sleeve: str, side: int, price: float, qty: int = 1) -> dict:
    return {"ts": day + MOC, "symbol": "ES", "sleeve": sleeve,
            "side": side, "qty": qty, "price": price}


def test_multiday_hold_is_realised():
    """THE REPRODUCTION. Buy Wed, sell the following Mon, +50pt. On the broken
    walk the position is dropped at each session boundary and the sleeve scores
    +0.00 -- indistinguishable from never having traded."""
    q = FakeQDB([_fill("2026-07-01", "ES:ibs", +1, 5000.0),
                 _fill("2026-07-06", "ES:ibs", -1, 5050.0)])
    out = paper_pnl(q)
    total = out.loc[out.sleeve == "ES:ibs", "pnl"].sum()
    assert total == pytest.approx(50.0), (
        f"multi-day hold realised {total:+.2f}pt, expected +50.00 -- the "
        f"position was dropped at the session boundary")


def test_multiday_pnl_lands_on_the_exit_session():
    """P&L belongs to the session the trade CLOSED in. G5/G8 count sessions, so
    smearing it across the hold would invent sessions that never had a result."""
    q = FakeQDB([_fill("2026-07-01", "ES:ibs", +1, 5000.0),
                 _fill("2026-07-06", "ES:ibs", -1, 5050.0)])
    out = paper_pnl(q).set_index("day")
    assert out.loc["2026-07-06", "pnl"] == pytest.approx(50.0)
    if "2026-07-01" in out.index:
        assert out.loc["2026-07-01", "pnl"] == pytest.approx(0.0), (
            "the entry session must show no realised P&L")


def test_multiday_loss_is_realised_too():
    """A losing swing must be just as visible -- otherwise the fix would flatter
    the sleeve rather than measure it."""
    q = FakeQDB([_fill("2026-07-01", "ES:ibs", +1, 5000.0),
                 _fill("2026-07-08", "ES:ibs", -1, 4970.0)])
    out = paper_pnl(q)
    assert out.pnl.sum() == pytest.approx(-30.0)


def test_intraday_sleeve_is_unchanged():
    """The control. Same-session round trips must score exactly as before."""
    q = FakeQDB([_fill("2026-07-01", "ES:onbreak", +1, 5000.0),
                 _fill("2026-07-01", "ES:onbreak", -1, 5010.0)])
    out = paper_pnl(q)
    assert out.pnl.sum() == pytest.approx(10.0)


def test_sleeves_do_not_bleed_into_each_other():
    """Carrying position across sessions must stay keyed per sleeve: two sleeves
    holding opposite positions over the same days must not net out."""
    q = FakeQDB([_fill("2026-07-01", "ES:ibs", +1, 5000.0),
                 _fill("2026-07-01", "ES:dipbuy", -1, 5000.0),
                 _fill("2026-07-06", "ES:ibs", -1, 5050.0),
                 _fill("2026-07-06", "ES:dipbuy", +1, 5050.0)])
    out = paper_pnl(q)
    assert out.loc[out.sleeve == "ES:ibs", "pnl"].sum() == pytest.approx(+50.0)
    assert out.loc[out.sleeve == "ES:dipbuy", "pnl"].sum() == pytest.approx(-50.0)


def test_position_still_open_realises_nothing():
    """An unclosed hold is not a result. It must not be marked to anything --
    the gate scores REALISED P&L only."""
    q = FakeQDB([_fill("2026-07-01", "ES:ibs", +1, 5000.0)])
    out = paper_pnl(q)
    assert out.pnl.sum() == pytest.approx(0.0)
