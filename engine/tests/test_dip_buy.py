"""DipBuyStrategy: RTH gate, session reset, entry on a flush->band touch, and
scale/stop/vwap/moc management. Plus the RegimeGate mean-reversion complement."""
import pandas as pd

from engine.core.events import Bar
from engine.core.orders import Order
from engine.strategies.dip_buy import DipBuyStrategy, _Pos


def ts(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return int(pd.Timestamp(f"{day} {h:02d}:{m:02d}", tz="America/New_York").value)


def bar(day, hhmm, o, h, l, c, v=100):
    return Bar(ts(day, hhmm), "1m", o, h, l, c, v)


def test_rth_gate_ignores_overnight():
    s = DipBuyStrategy("ES")
    assert s.on_bar(bar("2026-03-02", "03:00", 5000, 5001, 4999, 5000)) == []
    assert s.cum_v == 0.0                              # no state change off-hours


def test_session_reset_and_prev_close():
    s = DipBuyStrategy("ES")
    s.on_bar(bar("2026-03-02", "09:30", 5000, 5001, 4999, 5000))
    s.on_bar(bar("2026-03-02", "15:59", 5010, 5011, 5009, 5010))
    assert s.cum_v > 0
    s.on_bar(bar("2026-03-03", "09:30", 5010, 5011, 5009, 5010))   # new day
    assert s._prev_close == 5010.0                     # carried prior RTH close
    assert s.cum_v == 100.0                            # cumulative reset to this bar


def test_enters_long_on_flush_then_band_touch():
    """White-box: set session VWAP~5030 / sigma~10 (band VWAP-2s ~5010) with a
    flush low (5005, aged) below the band and recovery lows (5015) above it, then
    feed a bar that dips to touch the band and closes above the flush low."""
    from collections import deque
    s = DipBuyStrategy("ES")
    s._day = "2026-03-02"
    s._prev_close = None                              # force setup A (VWAP band)
    s.cum_v = 100_000.0
    s.cum_pv = 5030.0 * 100_000.0
    s.cum_d2 = (10.0 ** 2) * 100_000.0                # sigma = 10 -> band ~5010
    his = [(5030.0, 5005.0)] * 20 + [(5030.0, 5015.0)] * 25   # old flush low, recent >band
    s.his = deque(his, maxlen=90)
    s.since_touch_lo = deque([5015.0] * 16, maxlen=16)
    s.since_touch_hi = deque([5030.0] * 16, maxlen=16)
    out = s.on_bar(bar("2026-03-02", "11:00", 5012, 5012, 5008, 5011))
    assert out and out[0].side == 1
    assert "dip" in out[0].tag and "entry" in out[0].tag
    assert s.trade is not None and s.trade.dir == 1
    assert 5005 < s.trade.entry < 5015              # entered at the band, above the flush low


def test_manage_scalp_then_vwap_runner():
    s = DipBuyStrategy("ES")
    s.pos = 2
    s.trade = _Pos(dir=1, entry=5000.0, stop=4994.0, runner_tgt=5010.0, size=2,
                   remaining=2, scalp_px=5004.0)
    # bar hits +4 scalp -> half off, stop to BE
    out = s.on_bar(bar("2026-03-02", "11:00", 5001, 5005, 5000, 5004))
    assert out and out[0].tag == "dip-scale" and out[0].reduce_only
    assert s.trade.scalped and s.trade.stop == 5000.0
    # next bar reaches VWAP runner target -> exit remainder
    out = s.on_bar(bar("2026-03-02", "11:01", 5005, 5011, 5004, 5010))
    assert out and out[0].tag == "dip-vwap"
    assert s.trade is None


def test_manage_stop_out():
    s = DipBuyStrategy("ES")
    s.pos = 2
    s.trade = _Pos(dir=1, entry=5000.0, stop=4994.0, runner_tgt=5010.0, size=2,
                   remaining=2, scalp_px=5004.0)
    out = s.on_bar(bar("2026-03-02", "11:00", 5000, 5001, 4993, 4994))
    assert out and out[0].tag == "dip-stop" and out[0].qty == 2
    assert s.trade is None


def test_manage_moc_flatten():
    s = DipBuyStrategy("ES")
    s.pos = 2
    s.trade = _Pos(dir=1, entry=5000.0, stop=4994.0, runner_tgt=5020.0, size=2,
                   remaining=2, scalp_px=5004.0)
    out = s.on_bar(bar("2026-03-02", "15:59", 5001, 5002, 5000, 5001))
    assert out and out[0].tag == "dip-moc"
    assert s.trade is None


# ── regime gate: dip-buy is the complement of the trend gate ──────────────
class FakeGamma:
    def __init__(self, m):
        self.m = m

    def gexp_prev(self, day):
        return self.m.get(day)

    def is_short_gamma(self, day, max_pctl=1 / 3):
        v = self.m.get(day)
        return None if v is None else v <= max_pctl


def test_regime_mr_complement():
    from engine.core.regime import RegimeGate
    SHORT, MID = "2026-03-02", "2026-03-03"
    g = RegimeGate(FakeGamma({SHORT: 0.10, MID: 0.50}))
    # dip-buy: BLOCKED in short gamma, ALLOWED in mid/long
    assert g.blocks("DipBuyStrategy", 0, 1, 2, ts(SHORT, "10:00"))
    assert not g.blocks("DipBuyStrategy", 0, 1, 2, ts(MID, "10:00"))
    # trend: exact opposite
    assert not g.blocks("IgnitionStrategy", 0, 1, 2, ts(SHORT, "10:00"))
    assert g.blocks("IgnitionStrategy", 0, 1, 2, ts(MID, "10:00"))
    # dip-buy exits always pass even in short gamma
    assert not g.blocks("DipBuyStrategy", 3, -1, 3, ts(SHORT, "10:00"))
