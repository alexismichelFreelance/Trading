"""The live replay must emit BookFlow, or two live sleeves are untestable.

`_fetch_live` reconstructed Trades and Bars and nothing else. ignition and flow
both need per-second book pressure -- flow's whole signal IS the aggressor delta
and the bid/ask add-cancel imbalance -- so in the replay they saw no BookFlow,
took no trades, and produced no rows at all. Not a bad result: an ABSENT one.

That is how ES:ignition_gex and ES:flow_gex could sit in the roster for weeks
with their gate never once evaluated against a counterfactual, and why the
2026-08-15 comparison of the gexp gate against the local-sign gate could only
speak about onbreak. Two of the three sleeves under test could not run.

The data was always there. claude_sec_live carries exactly the four BookFlow
fields (bid_cancel, ask_cancel, bid_add, ask_add) plus pxc, 1.69M rows back to
2026-07-14 -- recorded for this purpose and then never wired in.

The kind field had two values and the decoder's `else` meant Bar. A third kind
therefore has to be an EXPLICIT branch: adding one and leaving the else alone
would silently emit BookFlow rows as Bars with garbage OHLC.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from engine.core.events import Bar, BookFlow, Trade  # noqa: E402
from portfolio_replay import _events_from  # noqa: E402


def _df(rows):
    return pd.DataFrame(rows, columns=["ts", "kind", "a", "b", "c", "d", "e"])


def test_bookflow_rows_decode_to_bookflow():
    """THE REGRESSION: kind 2 fell through the else and became a Bar."""
    df = _df([[1_000, 2, 11.0, 22.0, 33.0, 44.0, 0]])
    ev = _events_from(df, "ES")
    assert len(ev) == 1
    e = ev[0]
    assert isinstance(e, BookFlow), f"kind 2 decoded as {type(e).__name__}"
    assert (e.bid_cancel, e.ask_cancel, e.bid_add, e.ask_add) == (11.0, 22.0, 33.0, 44.0)
    assert e.symbol == "ES"


def test_trades_and_bars_are_unchanged():
    df = _df([[1_000, 0, 1.0, 2.0, 0.5, 1.5, 100],
              [1_000, 1, 7.25, 3, 1, 0.0, 0]])
    ev = _events_from(df, "ES")
    kinds = [type(x).__name__ for x in ev]
    assert kinds == ["Bar", "Trade"], kinds
    assert ev[0].o == 1.0 and ev[0].v == 100
    assert ev[1].price == 7.25 and ev[1].size == 3


def test_a_bar_still_precedes_a_trade_at_the_same_instant():
    """Coarse features must update before fine ones -- the existing lexsort
    contract. A third kind must not disturb it."""
    df = _df([[5, 1, 7.0, 1, 1, 0.0, 0],
              [5, 0, 1.0, 2.0, 0.5, 1.5, 10]])
    assert [type(x).__name__ for x in _events_from(df, "ES")] == ["Bar", "Trade"]


def test_bookflow_sorts_after_the_bar_at_the_same_instant():
    """BookFlow is per-SECOND aggregate state; like a trade it is finer than the
    bar it falls inside, so it must not jump ahead of it."""
    df = _df([[5, 2, 1.0, 1.0, 1.0, 1.0, 0],
              [5, 0, 1.0, 2.0, 0.5, 1.5, 10]])
    assert [type(x).__name__ for x in _events_from(df, "ES")][0] == "Bar"


def test_an_unknown_kind_raises_rather_than_becoming_a_bar():
    """The `else means Bar` default is what let this bug exist silently. A kind
    the decoder does not know must fail loudly, not be mis-typed into the tape."""
    df = _df([[1, 9, 0.0, 0.0, 0.0, 0.0, 0]])
    try:
        _events_from(df, "ES")
    except (ValueError, KeyError):
        return
    raise AssertionError("an unknown event kind was silently decoded")
