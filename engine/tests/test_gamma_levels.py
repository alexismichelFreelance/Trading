"""GammaLevels causal loader + painter gamma-line drawing."""
import asyncio

from engine.features.gamma_levels import GammaLevels
from engine.painters import PaintController


class FakeQ:
    def df(self, sql):
        import pandas as pd
        return pd.DataFrame({
            "ts": pd.to_datetime(["2026-07-08", "2026-07-09", "2026-07-10"], utc=True),
            "put_wall": [7500.0, 7500.0, 7500.0],
            "call_wall": [7500.0, 7500.0, 7550.0],
            "zero_gamma": [None, 7573.31, 7561.80],
            "net_sign": [-1, 1, 1],
        })


def test_levels_prev_is_causal_and_basis_shifted():
    gl = GammaLevels(FakeQ())
    # trading 2026-07-10 uses 2026-07-09's levels (the PRIOR row)
    lv = gl.levels_prev("2026-07-10", basis=52.0)
    assert lv["sess"] == "2026-07-09"
    assert lv["put_wall"] == 7500.0 + 52.0
    assert lv["call_wall"] == 7500.0 + 52.0
    assert abs(lv["flip"] - (7573.31 + 52.0)) < 1e-6
    assert lv["net_sign"] == 1


def test_levels_prev_handles_missing_flip_and_no_prior():
    gl = GammaLevels(FakeQ())
    lv = gl.levels_prev("2026-07-09")            # prior = 07-08, short gamma, flip None
    assert lv["net_sign"] == -1 and lv["flip"] is None
    assert gl.levels_prev("2026-07-08") is None  # nothing before the first row


class RecordPainter:
    def __init__(self):
        self.hlines = {}
        self.texts = {}

    async def hline(self, tag, price, color=""):
        self.hlines[tag] = (price, color)

    async def text(self, tag, ts, price, label, color=""):
        self.texts[tag] = (price, label)


def test_painter_draws_three_gamma_lines():
    rp = RecordPainter()
    pc = PaintController(rp, [])
    pc.set_gamma_levels(dict(put_wall=7552.0, call_wall=7602.0, flip=7625.0,
                             net_sign=1, basis=52.0))
    asyncio.run(pc._paint_gamma(123))
    assert rp.hlines["eng-gex-pw"][0] == 7552.0
    assert rp.hlines["eng-gex-cw"][0] == 7602.0
    assert rp.hlines["eng-gex-flip"][0] == 7625.0
    assert "put wall" in rp.texts["eng-gex-pw-t"][1]
    # No bar has been fed, so the painter has no price -- and the regime is a
    # statement about where PRICE is. It used to answer from net_sign (the sign
    # of the whole option book), which across 44 rebuilt CBOE payloads
    # disagreed with the regime at spot on 25 of them.
    assert "regime unknown" in rp.texts["eng-gex-flip-t"][1]


def test_the_flip_label_follows_price_once_there_is_a_price():
    """Below the flip is SHORT gamma however positive the book totals to; above
    it is long. Same levels, two prices, two labels."""
    for px, want in ((7600.0, "SHORT-gamma"), (7650.0, "long-gamma")):
        rp = RecordPainter()
        pc = PaintController(rp, [])
        pc.set_gamma_levels(dict(put_wall=7552.0, call_wall=7602.0, flip=7625.0,
                                 net_sign=1, basis=52.0))
        pc._last_px = px
        asyncio.run(pc._paint_gamma(123))
        assert want in rp.texts["eng-gex-flip-t"][1], (
            f"price {px} against a flip at 7625 labelled "
            f"{rp.texts['eng-gex-flip-t'][1]!r}")


def test_painter_short_gamma_no_flip_line():
    rp = RecordPainter()
    pc = PaintController(rp, [])
    pc.set_gamma_levels(dict(put_wall=7552.0, call_wall=7552.0, flip=None,
                             net_sign=-1, basis=52.0))
    asyncio.run(pc._paint_gamma(123))
    assert "eng-gex-flip" not in rp.hlines           # no flip when short-gamma/None
    assert "eng-gex-pw" in rp.hlines and "eng-gex-cw" in rp.hlines


def test_painter_no_levels_is_noop():
    rp = RecordPainter()
    pc = PaintController(rp, [])
    asyncio.run(pc._paint_gamma(123))
    assert rp.hlines == {}
