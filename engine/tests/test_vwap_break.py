"""VwapBreakStrategy: enters WITH a break of the session-VWAP band, bails back
to the VWAP mean, is gamma-gated to short-gamma days, and flats at EOD."""
import pandas as pd

from engine.core.events import Bar, PositionUpdate
from engine.strategies.vwap_break import VwapBreakStrategy


def _b(et_str, o, h, l, c, v=1000):
    ts = int(pd.Timestamp(et_str, tz="America/New_York").value)
    return Bar(ts, "1m", o, h, l, c, v, "ES")


def _drive(strat, bars):
    orders, pos = [], 0
    for b in bars:
        for o in strat.on_bar(b):
            orders.append(o)
            pos += o.side * o.qty
            strat.on_position(PositionUpdate(b.ts, strat.symbol, pos, 0.0))
    return orders, pos


class _FakeGamma:
    def __init__(self, sg):
        self.sg = sg

    def is_short_gamma(self, day, max_pctl=1 / 3):
        return self.sg


def _session(breakout: bool):
    """Build an RTH session: a balanced first 45m (builds VWAP + sigma), then
    either a decisive up-break or a flat continuation."""
    bars = [_b("2026-07-23 09:30", 7500, 7502, 7498, 7500)]
    # 09:31..10:14 oscillate around 7500 to grow a non-zero sigma band
    for i in range(31, 75):
        hh, mm = divmod(i, 60)
        px = 7500 + (6 if i % 2 else -6)
        bars.append(_b(f"2026-07-23 {9+hh:02d}:{mm:02d}", 7500, px + 1, px - 1, px))
    if breakout:                                   # 10:15+ break well above the band and run
        for i, px in enumerate([7515, 7525, 7535, 7545]):
            bars.append(_b(f"2026-07-23 10:{15+i:02d}", px - 2, px + 1, px - 3, px))
    return bars


def test_short_gamma_takes_the_upside_break():
    s = VwapBreakStrategy("ES", gamma=_FakeGamma(True))
    orders, _ = _drive(s, _session(breakout=True))
    entry = next((o for o in orders if o.tag == "entry-vwapbreak"), None)
    assert entry is not None and entry.side == 1        # bought the break, with it


def test_long_gamma_stands_down():
    s = VwapBreakStrategy("ES", gamma=_FakeGamma(False))
    orders, _ = _drive(s, _session(breakout=True))
    assert not any(o.tag == "entry-vwapbreak" for o in orders)   # no short-gamma -> no trade


def test_no_break_no_trade():
    s = VwapBreakStrategy("ES", gamma=_FakeGamma(True))
    orders, _ = _drive(s, _session(breakout=False))
    assert not any(o.tag == "entry-vwapbreak" for o in orders)


def test_bail_to_vwap_and_eod_flat():
    s = VwapBreakStrategy("ES", gamma=_FakeGamma(True))
    bars = _session(breakout=True)
    # after the break, price collapses back through the VWAP mean -> vwap-fail exit
    bars += [_b("2026-07-23 10:20", 7545, 7546, 7495, 7498)]
    bars += [_b("2026-07-23 15:59", 7500, 7501, 7499, 7500)]      # EOD safety
    orders, pos = _drive(s, bars)
    assert any(o.tag == "vwb-vwap-fail" for o in orders)
    assert pos == 0                                              # flat by the close
