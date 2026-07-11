"""RegimeGate unit tests: trend-sleeve entries suppressed on non-short-gamma
days; exits/reduces and ungated sleeves always pass; fail-open on unknown."""
import pandas as pd

from engine.core.regime import RegimeGate, increases_exposure

SHORT_DAY = "2026-03-02"     # gexp_prev <= 1/3
LONG_DAY = "2026-03-03"      # gexp_prev  > 1/3
UNK_DAY = "2099-01-01"       # no GEX row


def ts_et(day: str, hhmm: str = "10:00") -> int:
    return int(pd.Timestamp(f"{day} {hhmm}", tz="America/New_York").value)


class FakeGamma:
    def __init__(self, mapping):
        self.m = mapping                     # day -> gexp_prev (or absent)

    def gexp_prev(self, day):
        return self.m.get(day)

    def is_short_gamma(self, day, max_pctl=1 / 3):
        v = self.m.get(day)
        return None if v is None else v <= max_pctl


G = FakeGamma({SHORT_DAY: 0.10, LONG_DAY: 0.80})


def test_increases_exposure():
    assert increases_exposure(0, 1, 3)          # flat -> long: entry
    assert increases_exposure(2, 1, 1)          # add to long
    assert not increases_exposure(3, -1, 2)     # reduce long
    assert not increases_exposure(3, -1, 3)     # flatten
    assert not increases_exposure(3, -1, 5)     # partial flip +3 -> -2 (smaller abs)
    assert increases_exposure(3, -1, 10)        # overshoot flip +3 -> -7 (larger abs)


def test_trend_entry_blocked_on_long_gamma():
    g = RegimeGate(G)
    assert g.blocks("IgnitionStrategy", 0, 1, 1, ts_et(LONG_DAY))
    assert g.blocks("OpenDriveStrategy", 0, -1, 1, ts_et(LONG_DAY))
    assert g.blocks("FlowFollowingStrategy", 0, 1, 2, ts_et(LONG_DAY))


def test_trend_entry_allowed_on_short_gamma():
    g = RegimeGate(G)
    assert not g.blocks("IgnitionStrategy", 0, 1, 1, ts_et(SHORT_DAY))
    assert not g.blocks("FlowFollowingStrategy", 0, -1, 3, ts_et(SHORT_DAY))


def test_exits_always_pass_even_on_long_gamma():
    g = RegimeGate(G)
    # long 3, selling to reduce/flatten on a long-gamma day -> never blocked
    assert not g.blocks("IgnitionStrategy", 3, -1, 2, ts_et(LONG_DAY))
    assert not g.blocks("IgnitionStrategy", 3, -1, 3, ts_et(LONG_DAY))


def test_ungated_sleeves_never_blocked():
    g = RegimeGate(G)
    assert not g.blocks("ZoneLifecycleStrategy", 0, 1, 5, ts_et(LONG_DAY))
    assert not g.blocks("IBSSwingStrategy", 0, 1, 2, ts_et(LONG_DAY))


def test_fail_open_on_unknown_regime():
    g = RegimeGate(G)
    assert not g.blocks("IgnitionStrategy", 0, 1, 1, ts_et(UNK_DAY))


def test_disabled_gate_passes_everything():
    g = RegimeGate(G, enabled=False)
    assert not g.blocks("IgnitionStrategy", 0, 1, 1, ts_et(LONG_DAY))


def test_no_gamma_source_passes_everything():
    g = RegimeGate(None)
    assert not g.blocks("IgnitionStrategy", 0, 1, 1, ts_et(LONG_DAY))
