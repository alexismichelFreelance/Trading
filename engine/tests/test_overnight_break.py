"""OvernightBreakStrategy: breaks the overnight range WITH the break, refuses
the 09:30-10:00 fakeout window, bails when price closes back inside, and is
gamma-gated. The 2026-07-24 bear-trap is the motivating regression."""
import pandas as pd

from engine.core.events import Bar, PositionUpdate
from engine.strategies.overnight_break import OvernightBreakStrategy


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


def _overnight():
    """00:00-09:29 ET: range roughly [7430, 7470]."""
    bars = []
    for i in range(0, 540, 10):                    # every 10m through the night
        hh, mm = divmod(i, 60)
        px = 7450 + (18 if (i // 10) % 2 else -18)
        bars.append(_b(f"2026-07-24 {hh:02d}:{mm:02d}", 7450, px + 2, px - 2, px))
    return bars


def test_takes_upside_break_after_ten():
    bars = _overnight() + [
        _b("2026-07-24 09:45", 7460, 7465, 7455, 7460),    # inside range, pre-10:00
        _b("2026-07-24 10:05", 7470, 7490, 7469, 7488),    # clean break above
    ]
    s = OvernightBreakStrategy("ES", gamma=_FakeGamma(True))
    orders, _ = _drive(s, bars)
    e = next((o for o in orders if o.tag == "entry-onbreak"), None)
    assert e is not None and e.side == 1


def test_refuses_the_pre_ten_fakeout():
    """The 2026-07-24 pathology: a break inside the 09:30-10:00 window is the
    trap. It must be ignored, and a later break back the OTHER way is fine."""
    bars = _overnight() + [
        _b("2026-07-24 09:40", 7440, 7442, 7420, 7422),    # pokes BELOW pre-10:00
    ]
    s = OvernightBreakStrategy("ES", gamma=_FakeGamma(True))
    orders, _ = _drive(s, bars)
    assert not any(o.tag == "entry-onbreak" for o in orders)


def test_long_gamma_stands_down():
    bars = _overnight() + [_b("2026-07-24 10:05", 7470, 7490, 7469, 7488)]
    s = OvernightBreakStrategy("ES", gamma=_FakeGamma(False))
    orders, _ = _drive(s, bars)
    assert not any(o.tag == "entry-onbreak" for o in orders)


def test_failed_break_bails_and_eod_flat():
    bars = _overnight() + [
        _b("2026-07-24 10:05", 7470, 7490, 7469, 7488),    # break up -> long
        _b("2026-07-24 10:20", 7488, 7489, 7455, 7458),    # closes back INSIDE
        _b("2026-07-24 15:59", 7460, 7461, 7459, 7460),
    ]
    s = OvernightBreakStrategy("ES", gamma=_FakeGamma(True))
    orders, pos = _drive(s, bars)
    assert any(o.tag == "onb-range-fail" for o in orders)
    assert pos == 0


def test_quiet_night_no_trade():
    bars = [_b(f"2026-07-24 0{i//60}:{i%60:02d}", 7450, 7451, 7449, 7450)
            for i in range(0, 300, 10)]
    bars.append(_b("2026-07-24 10:05", 7452, 7460, 7451, 7458))
    s = OvernightBreakStrategy("ES", gamma=_FakeGamma(True))
    orders, _ = _drive(s, bars)
    assert not any(o.tag == "entry-onbreak" for o in orders)   # range < min_range
