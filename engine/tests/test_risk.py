"""RiskSupervisor unit tests — every rule that would have stopped the
2026-07-09 16:09 burst, plus caps, rate limit, lockout, EOD, kill switch."""
import pandas as pd

from engine.core.orders import Order
from engine.core.risk import RiskConfig, RiskSupervisor

BUY, SELL = 1, -1


def ts_et(hhmm: str, day: str = "2026-07-09") -> int:
    return int(pd.Timestamp(f"{day} {hhmm}", tz="America/New_York").value)


class FakeNow:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


RTH = ts_et("14:00")


def test_duplicate_flatten_suppressed_in_flight():
    r = RiskSupervisor(RiskConfig())
    o1 = r.vet(1, "zones", Order("ES", SELL, 21, reduce_only=True, tag="flat"), RTH, 21)
    assert o1 is not None and o1.qty == 21
    r.on_submit(1, o1)
    # identical flatten while the first is unfilled -> the July 9 bug -> DENY
    o2 = r.vet(1, "zones", Order("ES", SELL, 21, reduce_only=True, tag="flat"), RTH, 21)
    assert o2 is None
    # first fill lands; book flat; a further reduce is dropped on pos==0
    r.on_fill(1, o1.order_id, -21, 5000.0)
    o3 = r.vet(1, "zones", Order("ES", SELL, 21, reduce_only=True, tag="flat"), RTH, 0)
    assert o3 is None


def test_partial_inflight_reduce_clamps_remainder():
    r = RiskSupervisor(RiskConfig())
    o1 = r.vet(1, "z", Order("ES", SELL, 8, reduce_only=True), RTH, 21)
    r.on_submit(1, o1)
    o2 = r.vet(1, "z", Order("ES", SELL, 21, reduce_only=True), RTH, 21)
    assert o2 is not None and o2.qty == 13          # 21 - 8 already working


def test_reduce_semantics_preserved_without_pending():
    r = RiskSupervisor(RiskConfig())
    assert r.vet(1, "s", Order("ES", SELL, 5, reduce_only=True), RTH, 0) is None
    o = r.vet(1, "s", Order("ES", SELL, 3, reduce_only=True), RTH, 1)
    assert o is not None and o.qty == 1              # clamp, as before
    assert r.vet(1, "s", Order("ES", BUY, 1, reduce_only=True), RTH, 1) is None


def test_entry_stacking_respects_sleeve_cap():
    r = RiskSupervisor(RiskConfig(max_pos_per_sleeve=10))
    o1 = r.vet(1, "s", Order("ES", BUY, 7), RTH, 0)
    assert o1.qty == 7
    r.on_submit(1, o1)
    o2 = r.vet(1, "s", Order("ES", BUY, 7), RTH, 0)   # confirmed still 0, 7 in flight
    assert o2 is not None and o2.qty == 3
    r.on_submit(1, o2)
    assert r.vet(1, "s", Order("ES", BUY, 1), RTH, 0) is None


def test_account_gross_cap_spans_sleeves():
    r = RiskSupervisor(RiskConfig(max_account_gross=10))
    a = r.vet(1, "a", Order("ES", BUY, 7), RTH, 0)
    r.on_submit(1, a)
    b = r.vet(2, "b", Order("ES", SELL, 7), RTH, 0)
    assert b is not None and b.qty == 3               # gross 7 + 3 = 10
    r.on_submit(2, b)
    assert r.vet(3, "c", Order("ES", BUY, 1), RTH, 0) is None


def test_rate_limit_per_strategy():
    now = FakeNow()
    r = RiskSupervisor(RiskConfig(rate_max_orders=3, rate_window_s=5.0), now_fn=now)
    for i in range(3):
        o = r.vet(1, "s", Order("ES", SELL, 1), RTH, 0)
        assert o is not None
        r.on_submit(1, o)
    assert r.vet(1, "s", Order("ES", SELL, 1), RTH, 0) is None      # 4th in window
    assert r.vet(2, "other", Order("ES", BUY, 1), RTH, 0) is not None
    now.t = 6.0                                                     # window rolls
    assert r.vet(1, "s", Order("ES", SELL, 1), RTH, 0) is not None


def test_entry_lockout_blocks_entries_not_reduces():
    r = RiskSupervisor(RiskConfig(entry_lockout_et=(15, 45)))
    late = ts_et("15:59")
    assert r.vet(1, "z", Order("ES", BUY, 7), late, 0) is None       # the July 9 entries
    o = r.vet(1, "z", Order("ES", SELL, 5, reduce_only=True), late, 5)
    assert o is not None and o.qty == 5                              # exits still allowed
    assert r.vet(1, "z", Order("ES", BUY, 1), ts_et("15:44"), 0) is not None
    assert r.vet(1, "z", Order("ES", BUY, 1), ts_et("18:30"), 0) is not None  # evening ok


def test_eod_flatten_once_then_block_then_retry():
    now = FakeNow()
    r = RiskSupervisor(RiskConfig(eod_flatten_et=(15, 58), reemit_s=10.0,
                                  inflight_ttl_s=20.0), now_fn=now)
    books = [(1, "zones", "ES", 7), (2, "ign", "ES", 0)]
    out = r.on_market(ts_et("15:57"), 5000.0, books)
    assert out == []
    out = r.on_market(ts_et("15:58"), 5000.0, books)
    assert len(out) == 1 and out[0][0] == 1
    sid, o = out[0]
    assert o.reduce_only and o.side == SELL and o.qty == 7 and o.tag == "risk_eod"
    r.on_submit(sid, o)
    # strategy orders now denied until the evening
    assert r.vet(1, "zones", Order("ES", BUY, 1), ts_et("15:59"), 7) is None
    # no re-emit while the flatten is in flight
    now.t = 11.0
    assert r.on_market(ts_et("15:59"), 5000.0, books) == []
    # flatten lost: TTL expires the pending; retry cadence re-emits
    now.t = 31.0
    out = r.on_market(ts_et("15:59:30"), 5000.0, books)
    assert len(out) == 1 and out[0][1].qty == 7


def test_swing_sleeve_enters_late_and_survives_eod_flatten():
    """IBS enters at 15:59 and holds overnight: exempt from the entry lockout and
    the EOD flatten; intraday sleeves are still flattened; kill switch still
    flattens IBS (catastrophe)."""
    cfg = RiskConfig(entry_lockout_et=(15, 45), eod_flatten_et=(15, 58),
                     daily_loss_halt=-3000.0, swing_sleeves=("IBSSwingStrategy",))
    r = RiskSupervisor(cfg)
    # EOD flatten fires at 15:58: flattens the intraday zones book, spares IBS
    books = [(1, "ZoneLifecycleStrategy", "ES", 3), (2, "IBSSwingStrategy", "ES", 2)]
    flat = r.on_market(ts_et("15:58"), 5000.0, books)
    ids = [sid for sid, _ in flat]
    assert 1 in ids and 2 not in ids                  # zones flattened, IBS spared
    assert flat[0][1].tag == "risk_eod"
    # IBS's 15:59 entry is allowed despite lockout + post-EOD
    o = r.vet(2, "IBSSwingStrategy", Order("ES", BUY, 1, tag="ibs-entry"),
              ts_et("15:59"), 0)
    assert o is not None and o.qty == 1
    # an intraday sleeve is still locked out / post-EOD
    assert r.vet(1, "ZoneLifecycleStrategy", Order("ES", BUY, 1), ts_et("15:59"), 0) is None


def test_kill_switch_flattens_and_halts_until_next_session():
    r = RiskSupervisor(RiskConfig(daily_loss_halt=-3000.0, point_value=50.0))
    # book: long 2 @ 5000, price falls to 4960 -> marked -80pt*50 = -4000
    e = Order("ES", BUY, 2)
    r.on_submit(1, e)
    r.on_fill(1, e.order_id, 2, 5000.0)
    out = r.on_market(RTH, 4960.0, [(1, "s", "ES", 2)])
    assert r.halted
    assert len(out) == 1 and out[0][1].tag == "risk_halt" and out[0][1].qty == 2
    assert r.vet(1, "s", Order("ES", BUY, 1), RTH, 2) is None
    assert r.vet(1, "s", Order("ES", SELL, 2, reduce_only=True), RTH, 2) is None
    # next ET session: reset
    nxt = ts_et("09:31", "2026-07-10")
    assert not r.vet(1, "s", Order("ES", BUY, 1), nxt, 0) is None
    assert not r.halted


def test_realized_ledger_tracks_flat_to_flat():
    r = RiskSupervisor(RiskConfig(point_value=50.0))
    e = Order("ES", BUY, 3)
    r.on_submit(1, e)
    r.on_fill(1, e.order_id, 3, 5000.0)
    x = Order("ES", SELL, 3, reduce_only=True)
    r.on_submit(1, x)
    r.on_fill(1, x.order_id, -3, 5004.0)
    assert abs(r.marked_pnl(9999.0) - 3 * 4 * 50.0) < 1e-9   # flat: px-independent


def test_pending_ttl_frees_reducible_qty():
    now = FakeNow()
    r = RiskSupervisor(RiskConfig(inflight_ttl_s=20.0), now_fn=now)
    o1 = r.vet(1, "s", Order("ES", SELL, 21, reduce_only=True), RTH, 21)
    r.on_submit(1, o1)
    assert r.vet(1, "s", Order("ES", SELL, 21, reduce_only=True), RTH, 21) is None
    now.t = 25.0                                             # fill never arrived
    o2 = r.vet(1, "s", Order("ES", SELL, 21, reduce_only=True), RTH, 21)
    assert o2 is not None and o2.qty == 21


# ── multi-instrument: dollar-notional caps + per-symbol point values ─────────

def test_sleeve_notional_cap_clamps_es_vs_nq():
    pv = {"ES": 50.0, "NQ": 20.0}
    r = RiskSupervisor(RiskConfig(point_usd=pv, max_sleeve_notional_usd=1_000_000))
    r.note_price("ES", 6000.0)      # 1 ES = $300k
    r.note_price("NQ", 20000.0)     # 1 NQ = $400k
    o = r.vet(1, "es_sleeve", Order("ES", BUY, 10), RTH, 0)
    assert o is not None and o.qty == 3          # floor(1M / 300k)
    o = r.vet(2, "nq_sleeve", Order("NQ", BUY, 10), RTH, 0)
    assert o is not None and o.qty == 2          # floor(1M / 400k)


def test_gross_notional_cap_sums_dollars_across_instruments():
    pv = {"ES": 50.0, "NQ": 20.0}
    r = RiskSupervisor(RiskConfig(point_usd=pv, max_gross_notional_usd=1_000_000))
    r.note_price("ES", 6000.0)
    r.note_price("NQ", 20000.0)
    o1 = r.vet(1, "es", Order("ES", BUY, 2), RTH, 0)     # $600k
    assert o1.qty == 2
    r.on_submit(1, o1)
    r.on_fill(1, o1.order_id, 2, 6000.0, "ES")
    # $400k headroom = 1 NQ contract
    o2 = r.vet(2, "nq", Order("NQ", BUY, 3), RTH, 0)
    assert o2 is not None and o2.qty == 1
    r.on_submit(2, o2)
    r.on_fill(2, o2.order_id, 1, 20000.0, "NQ")
    # no headroom left for anything
    assert r.vet(3, "es2", Order("ES", BUY, 1), RTH, 0) is None


def test_no_mark_price_denies_when_dollar_cap_set():
    r = RiskSupervisor(RiskConfig(max_sleeve_notional_usd=1_000_000))
    # GC never ticked: deny, never bypass
    assert r.vet(1, "gc", Order("GC", BUY, 1), RTH, 0) is None
    r.note_price("GC", 3300.0)
    assert r.vet(1, "gc", Order("GC", BUY, 1), RTH, 0) is not None


def test_kill_switch_uses_per_symbol_point_values():
    pv = {"ES": 50.0, "NQ": 20.0}
    r = RiskSupervisor(RiskConfig(point_usd=pv, daily_loss_halt=-5000.0))
    o = r.vet(1, "nq", Order("NQ", BUY, 1), RTH, 0)
    r.on_submit(1, o)
    r.on_fill(1, o.order_id, 1, 20000.0, "NQ")
    # NQ down 100 pts = -$2000 at $20/pt (would be -$5000 at ES's $50 -> halt)
    books = [(1, "nq", "NQ", 1)]
    acts = r.on_market(RTH, {"NQ": 19900.0}, books)
    assert not r.halted and acts == []
    # down 250 pts = -$5000 at $20/pt -> halt fires, flatten emitted
    acts = r.on_market(RTH, {"NQ": 19750.0}, books)
    assert r.halted
    assert len(acts) == 1 and acts[0][1].reduce_only and acts[0][1].symbol == "NQ"


def test_marked_pnl_prices_dict_and_missing_mark():
    pv = {"ES": 50.0, "NQ": 20.0}
    r = RiskSupervisor(RiskConfig(point_usd=pv))
    for sid, sym, px in ((1, "ES", 6000.0), (2, "NQ", 20000.0)):
        o = r.vet(sid, sym, Order(sym, BUY, 1), RTH, 0)
        r.on_submit(sid, o)
        r.on_fill(sid, o.order_id, 1, px, sym)
    # ES +10 pts ($500), NQ +50 pts ($1000)
    assert r.marked_pnl({"ES": 6010.0, "NQ": 20050.0}) == 1500.0
    # NQ mark missing -> its book skipped, not mispriced with the ES price
    assert r.marked_pnl({"ES": 6010.0}) == 500.0


def test_legacy_scalar_interface_unchanged():
    r = RiskSupervisor(RiskConfig(daily_loss_halt=-5000.0))
    o = r.vet(1, "s", Order("ES", BUY, 2), RTH, 0)
    r.on_submit(1, o)
    r.on_fill(1, o.order_id, 2, 5000.0)               # no symbol arg
    assert r.marked_pnl(4900.0) == -10000.0           # scalar price, 50 $/pt
    acts = r.on_market(RTH, 4900.0, [(1, "s", "ES", 2)])
    assert r.halted and len(acts) == 1
