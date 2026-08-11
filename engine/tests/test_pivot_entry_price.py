"""A pivot entry must be a resting limit AT the level it claims to fade.

pivot.py's docstring: "Execution = resting limit-style entries at pivots". The
code emitted a MARKET order:

    self.trade = {"dir": d, "entry": entry, ...}       # entry = the pivot level
    return [Order(self.symbol, d, size, tag=f"{tag}-entry")]   # ...market

So the position filled at whatever the tape was doing, while the sleeve recorded
the PIVOT as its entry and then derived stop and target from that. Two faults in
one line: the fill misses the level, and the risk arithmetic is measured from a
price the trade never had.

Measured on the verified grid, 5 ES sessions:
    piv-entry     n=9  median 6.67 pts from the nearest level, max 21.75
    pivrev-entry  n=4  median 8.75                                  (by design:
                       the reversal enters BEYOND the deep pivot by a margin)

A 21.75-point miss on a level-fading sleeve is not a fade. And a stop computed
as `piv + STOP_BUF` while filled 21 points away is not the risk it advertises.

The detection already proves the level was reachable -- `bar.h >= piv` for a
short, `bar.l <= piv` for a long -- so a limit resting at `piv` was fillable on
that bar.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.events import Bar  # noqa: E402
from engine.core.orders import OrderType  # noqa: E402
from engine.strategies.pivot import PivotStrategy  # noqa: E402


def _b(t, o, h, l, c, v=1000):
    return Bar(int(pd.Timestamp(t, tz="America/New_York").value), "1m",
               o, h, l, c, v, "ES")


def _armed_short():
    """Prior day + a down-trending night -> short bias, grid in place."""
    s = PivotStrategy("ES")
    s.on_bar(_b("2026-07-22 12:00", 7500, 7550, 7450, 7460))
    px = 7480.0
    for i in range(12):
        px -= 4
        s.on_bar(_b(f"2026-07-23 0{i // 60}:{i % 60:02d}", px + 1, px + 2, px - 1, px))
    for i in range(12, 40):
        hh, mm = divmod(i, 60)
        s.on_bar(_b(f"2026-07-23 0{hh}:{mm:02d}", 7436, 7438, 7434, 7436))
    s.on_bar(_b("2026-07-23 09:30", 7436, 7440, 7434, 7438))     # freeze bias
    return s


def test_directional_entry_is_a_limit_at_the_pivot():
    """THE REGRESSION."""
    s = _armed_short()
    orders = s.on_bar(_b("2026-07-23 09:45", 7470, 7490, 7465, 7478))
    ent = [o for o in orders if o.tag == "piv-entry"]
    assert ent, f"no entry; orders={[o.tag for o in orders]}"
    o = ent[0]
    assert o.type == OrderType.LIMIT, f"entered with a {o.type} order"
    assert o.limit_price is not None
    assert abs(o.limit_price - s.trade["entry"]) < 1e-9, (
        f"limit {o.limit_price} does not match the recorded entry "
        f"{s.trade['entry']} -- the stop/target are derived from the entry")


def test_the_limit_sits_on_a_grid_level():
    s = _armed_short()
    orders = s.on_bar(_b("2026-07-23 09:45", 7470, 7490, 7465, 7478))
    o = [x for x in orders if x.tag == "piv-entry"][0]
    assert any(abs(o.limit_price - g) < 1e-9 for g in s.grid), (
        f"limit {o.limit_price} is not one of the grid levels")


def test_the_reversal_entry_is_a_limit_too():
    """pivrev enters at the deep pivot; it may target a margin beyond, but the
    order still has to be priced rather than sprayed at the market."""
    s = _armed_short()
    s.on_bar(_b("2026-07-23 09:45", 7470, 7490, 7465, 7478))
    s.on_bar(_b("2026-07-23 10:00", 7470, 7472, 7420, 7425))
    orders = s.on_bar(_b("2026-07-23 10:30", 7360, 7365, 7320, 7340))
    rev = [o for o in orders if o.tag == "pivrev-entry"]
    if rev:                                   # only if the deep level was reached
        assert rev[0].type == OrderType.LIMIT, f"reversal used {rev[0].type}"
        assert abs(rev[0].limit_price - s.trade["entry"]) < 1e-9


def test_exits_stay_market_so_a_stop_can_never_be_left_resting():
    """Only ENTRIES become limits. An exit that rests is an exit that may not
    happen, which is how a stop turns into an unbounded loss."""
    s = _armed_short()
    s.on_bar(_b("2026-07-23 09:45", 7470, 7490, 7465, 7478))
    out = []
    for b in (_b("2026-07-23 10:00", 7470, 7472, 7420, 7425),
              _b("2026-07-23 15:59", 7460, 7465, 7455, 7460)):
        out += s.on_bar(b) or []
    for o in out:
        if "entry" not in (o.tag or ""):
            assert o.type == OrderType.MARKET, f"exit {o.tag} rested as {o.type}"
