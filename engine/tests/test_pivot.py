"""PivotStrategy: overnight bias read + pivot-fade execution + deep-pivot
reversal + EOD flat (the mechanical copy of the user's 2026-07-23 day)."""
import pandas as pd

from engine.core.events import Bar, PositionUpdate
from engine.features.pivots import daily_pivots
from engine.strategies.pivot import PivotStrategy


def _b(et_str, o, h, l, c, v=1000):
    ts = int(pd.Timestamp(et_str, tz="America/New_York").value)
    return Bar(ts, "1m", o, h, l, c, v, "ES")


def _drive(strat, bars):
    """Feed bars; simulate fills so self.pos tracks (as the engine would)."""
    orders = []
    pos = 0
    for b in bars:
        for o in strat.on_bar(b):
            orders.append(o)
            pos += o.side * o.qty
            strat.on_position(PositionUpdate(b.ts, strat.symbol, pos, 0.0))
    return orders, pos


# prior session 7550/7450/7460 -> known pivots
PIV = daily_pivots(7550.0, 7450.0, 7460.0)   # PP~7486.7 S1~7423.3 S3~7323.3 R1~7523.3


def _prior_day():
    return [_b("2026-07-22 12:00", 7500, 7550, 7450, 7460)]


def _overnight_down():
    """Asia/London: drop then sit low -> mostly below VWAP, trending down."""
    bars = []
    # 07-23 starts at ET midnight; feed 00:00..06:00 (well before 09:30)
    px = 7480.0
    for i in range(12):                          # steady drop 7480 -> 7436
        px -= 4
        bars.append(_b(f"2026-07-23 0{i//60}:{i%60:02d}", px + 1, px + 2, px - 1, px))
    for i in range(12, 40):                      # sit ~7436 (below VWAP) for a while
        hh, mm = divmod(i, 60)
        bars.append(_b(f"2026-07-23 0{hh}:{mm:02d}", 7436, 7438, 7434, 7436))
    return bars


def test_overnight_bias_short_on_down_trending_night():
    s = PivotStrategy("ES")
    _drive(s, _prior_day() + _overnight_down())
    # first RTH bar freezes the bias
    s.on_bar(_b("2026-07-23 09:30", 7436, 7440, 7434, 7438))
    assert s.bias == -1 and s.conviction >= 0.30


def test_neutral_bias_on_chop():
    s = PivotStrategy("ES")
    bars = _prior_day()
    for i in range(40):                          # oscillate around 7460 = wiggle
        hh, mm = divmod(i, 60)
        px = 7460 + (3 if i % 2 else -3)
        bars.append(_b(f"2026-07-23 0{hh}:{mm:02d}", 7460, px + 1, px - 1, px))
    _drive(s, bars)
    s.on_bar(_b("2026-07-23 09:30", 7460, 7462, 7458, 7460))
    assert s.bias == 0                           # low efficiency -> stand down


def test_short_entry_cover_reversal_and_eod_flat():
    s = PivotStrategy("ES")
    bars = _prior_day() + _overnight_down()
    orders, _ = _drive(s, bars)
    assert not orders                            # nothing trades overnight
    rth = [
        _b("2026-07-23 09:30", 7436, 7440, 7434, 7438),   # freeze bias (short)
        _b("2026-07-23 09:45", 7470, 7490, 7465, 7478),   # rally into PP -> SHORT
        _b("2026-07-23 10:00", 7470, 7472, 7420, 7425),   # drop through S1 -> COVER
        _b("2026-07-23 10:30", 7360, 7365, 7320, 7340),   # deep to S3 -> reversal LONG
        _b("2026-07-23 15:59", 7460, 7465, 7455, 7460),   # EOD -> flat
    ]
    orders, pos = _drive(s, rth)
    tags = [o.tag for o in orders]
    assert any(t == "piv-entry" for t in tags)                 # pivot fade short
    assert any(t == "piv-target" for t in tags)                # covered at next pivot
    assert any(t == "pivrev-entry" for t in tags)              # deep-pivot reversal long
    # first directional order is a SHORT (bias), reversal is a BUY
    entry = next(o for o in orders if o.tag == "piv-entry")
    rev = next(o for o in orders if o.tag == "pivrev-entry")
    assert entry.side == -1 and rev.side == 1
    assert pos == 0                                            # flat by the close


def test_multipivots_merges_day_week_month():
    from engine.features.pivots import MultiPivots
    mp = MultiPivots()

    def u(et_str, h, l, c):
        mp.update(int(pd.Timestamp(et_str, tz="America/New_York").value), h, l, c)

    u("2026-06-15 12:00", 7000, 6900, 6950)   # June
    u("2026-07-01 12:00", 7100, 7050, 7080)   # new month -> June completes
    u("2026-07-20 12:00", 7500, 7450, 7460)   # new week -> a July week completes
    u("2026-07-23 09:30", 7460, 7458, 7459)   # new day -> 07-20 completes
    g = mp.grid()
    assert {lbl.split("-")[0] for lbl in g.values()} == {"D", "W", "M"}
    dpp = next(px for px, lbl in g.items() if lbl == "D-PP")
    assert abs(dpp - (7500 + 7450 + 7460) / 3) < 0.01     # daily PP from 07-20
