"""OpenDrive ORB mode: on a short-gamma day it refuses the up-fakeout break and
shorts the resumed down-break — the 2026-07-23 scenario where blind 'drive' mode
went long at 10:00 and lost -41pt."""
import pandas as pd

from engine.core.events import Bar, PositionUpdate
from engine.strategies.open_drive import OpenDriveStrategy


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
    return orders


class _FakeGamma:
    def __init__(self, sg):
        self.sg = sg

    def is_short_gamma(self, day, max_pctl=1 / 3):
        return self.sg


# open drives UP 9:30->10:00 (the fakeout), then reverses and breaks the range low
_BARS = [
    _b("2026-07-23 09:30", 7440, 7442, 7438, 7440),
    _b("2026-07-23 09:45", 7450, 7472, 7448, 7470),   # up-drive (fakeout)
    _b("2026-07-23 10:00", 7470, 7471, 7468, 7469),   # OR frozen; no break yet
    _b("2026-07-23 10:15", 7460, 7462, 7435, 7436),   # breaks OR low -> resume down
]


def test_orb_short_gamma_refuses_fakeout_and_shorts_the_break():
    s = OpenDriveStrategy("ES", mode="orb", gamma=_FakeGamma(True))   # short gamma
    orders = _drive(s, _BARS)
    entry = next((o for o in orders if o.tag == "entry-orb"), None)
    assert entry is not None and entry.side == -1        # shorted the resume
    # crucially it did NOT buy the up-poke at 10:00
    assert all(o.side != 1 or o.reduce_only for o in orders)


def test_drive_mode_buys_the_fakeout_the_old_mistake():
    s = OpenDriveStrategy("ES", mode="drive")            # parity behavior
    orders = _drive(s, _BARS)
    entry = next(o for o in orders if o.tag == "entry-opendrive")
    assert entry.side == 1                               # blind long at 10:00


def test_orb_long_gamma_takes_up_break():
    # up drive that HOLDS and breaks higher, on a long-gamma day -> take the long
    bars = [
        _b("2026-07-23 09:30", 7440, 7442, 7438, 7440),
        _b("2026-07-23 09:45", 7450, 7460, 7448, 7458),
        _b("2026-07-23 10:00", 7458, 7460, 7456, 7459),   # OR ~[7438,7460]
        _b("2026-07-23 10:15", 7461, 7470, 7460, 7468),   # breaks OR high
    ]
    s = OpenDriveStrategy("ES", mode="orb", gamma=_FakeGamma(False))  # long gamma
    orders = _drive(s, bars)
    entry = next((o for o in orders if o.tag == "entry-orb"), None)
    assert entry is not None and entry.side == 1
