"""Scorecard twin comparison: per-day sleeve ledgers + raw-vs-_gex regime split."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from scorecard import _sleeve_ledgers, _twin_section  # noqa: E402


def _pf(rows):
    """rows: (ts_ns, sleeve, side, qty, price, day)"""
    df = pd.DataFrame(rows, columns=["ts", "sleeve", "side", "qty", "price", "day"])
    df["ts"] = pd.to_datetime(df.ts, unit="ns", utc=True)
    df["sq"] = df.side * df.qty
    return df


class FakeGamma:
    def __init__(self, m):
        self.m = m

    def is_short_gamma(self, day, max_pctl=1 / 3):
        return self.m.get(day)


def test_sleeve_ledgers_daily_realized():
    NS = 1_000_000_000
    pf = _pf([
        (1 * NS, "dipbuy", 1, 2, 6000.0, "2026-07-20"),
        (2 * NS, "dipbuy", -1, 2, 6005.0, "2026-07-20"),   # +10 pts day 1
        (3 * NS, "dipbuy", 1, 1, 6010.0, "2026-07-21"),
        (4 * NS, "dipbuy", -1, 1, 6002.0, "2026-07-21"),   # -8 pts day 2
        (5 * NS, "flow", -1, 3, 6000.0, "2026-07-20"),     # open short, never closed
    ])
    led = _sleeve_ledgers(pf)
    assert led["dipbuy"]["daily"] == {"2026-07-20": 10.0, "2026-07-21": -8.0}
    assert led["dipbuy"]["net"] == 0
    assert led["flow"]["daily"] == {} and led["flow"]["net"] == -3


def test_twin_section_regime_split(capsys):
    # dipbuy trades both days; dipbuy_gex (long-only) skipped the SHORT day,
    # where raw LOST -12 -> the filter's delta is +12 on SHORT, 0 on long.
    led = {
        "dipbuy": {"daily": {"2026-07-20": -12.0, "2026-07-21": 6.0},
                   "fills": 4, "days": 2, "net": 0},
        "dipbuy_gex": {"daily": {"2026-07-21": 6.0},
                       "fills": 2, "days": 1, "net": 0},
    }
    g = FakeGamma({"2026-07-20": True, "2026-07-21": False})  # SHORT, long
    _twin_section(led, gr=g)
    out = capsys.readouterr().out
    assert "RAW vs _gex twins" in out
    lines = [ln for ln in out.splitlines() if ln.strip().startswith("dipbuy")]
    short_row = next(ln for ln in lines if "SHORT" in ln)
    assert "-12.0" in short_row and "+0.0" in short_row and "+12.0" in short_row
    long_row = next(ln for ln in lines if " long" in ln)
    assert "+6.0" in long_row and "+0.0" in long_row.split()[-1]
    total = next(ln for ln in out.splitlines() if "TOTAL" in ln)
    assert "filter EARNS" in total          # -6 raw vs +6 gex -> +12 delta


def test_twin_section_no_pairs_silent(capsys):
    _twin_section({"zones": {"daily": {}, "fills": 0, "days": 0, "net": 0}})
    assert "twins" not in capsys.readouterr().out
