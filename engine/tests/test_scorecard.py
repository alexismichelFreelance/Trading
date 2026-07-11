"""scorecard avg-cost ledger: realized P&L, fills, peak position per day."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from scorecard import ledger_by_day


def _df(rows):
    # rows: (day, signed_qty, price); pv=50, usd=1 (ccy factor, as load_execs emits)
    return pd.DataFrame([dict(day=d, sq=q, px=p, pv=50.0, usd=1.0) for d, q, p in rows])


def test_flat_to_flat_realized_and_peak():
    g = _df([("D1", +2, 5000.0), ("D1", -2, 5004.0)])
    r = ledger_by_day(g)["D1"]
    assert abs(r["pnl"] - 2 * 4 * 50.0) < 1e-9       # +2 @5000 -> +4pt x2 x$50
    assert r["fills"] == 2 and r["peak"] == 2


def test_scale_in_then_out_average_cost():
    g = _df([("D1", +1, 5000.0), ("D1", +1, 5010.0), ("D1", -2, 5010.0)])
    r = ledger_by_day(g)["D1"]
    # avg 5005, exit 5010 x2 -> +5pt x2 x$50 = 500
    assert abs(r["pnl"] - 500.0) < 1e-9 and r["peak"] == 2


def test_short_side_and_carry_across_days():
    g = _df([("D1", -3, 5000.0), ("D2", +3, 4990.0)])
    led = ledger_by_day(g)
    assert led["D1"]["pnl"] == 0.0 and led["D1"]["peak"] == 3    # opened, not closed
    assert abs(led["D2"]["pnl"] - 3 * 10 * 50.0) < 1e-9          # covered +10pt


def test_peak_tracks_max_abs_position():
    g = _df([("D1", +5, 5000.0), ("D1", +8, 5001.0), ("D1", -13, 5002.0)])
    assert ledger_by_day(g)["D1"]["peak"] == 13
