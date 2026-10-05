"""Multi-lane asynchronous warmup backfill must not poison _eod_done.

The bug that led here: during multi-lane warmup, one lane (ES) goes live on the
CURRENT session day while another lane (NQ) is still replaying ITS historical
backfill from the PREVIOUS day. Once any lane is live, the engine runs the
supervisor on EVERY market event — including the warmer lane's backfill bars.
If one of those bars is stamped inside the flatten window (15:58-18:00 ET) of
the PRIOR day, the EOD flatten used to fire and set _eod_done=True for the
WRONG day. From then on every live order on the real session was denied
"post-EOD" and, worse, the real session never actually flattened.

This test drives a real two-lane engine: ES live on 2026-10-02 trading at
10:00 ET while NQ only replays 2026-10-01 backfill bars stamped 16:00 ET
(inside the flatten window). The NQ backfill must NOT set _eod_done, and ES's
live orders must keep filling.
"""
import asyncio
import json

from engine.adapters.brokers.ninjatrader import NinjaTraderBroker
from engine.adapters.feeds.ninjatrader import NinjaTraderFeed
from engine.core.blotter import Blotter
from engine.core.clock import WallClock
from engine.core.events import BUY
from engine.core.live_engine import LiveEngine
from engine.core.orders import Order
from engine.core.risk import RiskConfig, RiskSupervisor
from engine.core.timeutil import et_session_date

NS = 1_000_000_000


def _ns_et(y, m, d, hh, mm):
    import pandas as pd
    return int(pd.Timestamp(f"{y}-{m:02d}-{d:02d} {hh:02d}:{mm:02d}",
                            tz="America/New_York").value)


class OnTrade:
    """Orders 1-lot on every trade of its symbol."""

    def __init__(self, symbol):
        self.symbol = symbol
        self.trades = 0
        self.fills = []

    def on_bar(self, e):
        return []

    def on_trade(self, e):
        self.trades += 1
        return [Order(self.symbol, BUY, 1, tag=f"{self.symbol}-t")]

    def on_bookflow(self, e):
        return []

    def on_quote(self, e):
        return []

    def on_depth(self, e):
        return []

    def on_fill(self, e):
        self.fills.append(e)

    def on_position(self, e):
        return None


def _mk_market(msgs):
    async def market(reader, writer):
        for m in msgs:
            writer.write((json.dumps(m) + "\n").encode())
        await writer.drain()
        writer.close()
    return market


def _mk_broker(fills_by_symbol):
    async def broker(reader, writer):
        async for raw in reader:
            line = raw.strip()
            if not line:
                continue
            m = json.loads(line)
            if m.get("t") == "place":
                fills_by_symbol.setdefault(m["symbol"], 0)
                fills_by_symbol[m["symbol"]] += 1
                signed = m["side"] * m["qty"]
                writer.write((json.dumps(
                    {"t": "fill", "ts": 9 * NS, "order_id": m["order_id"],
                     "symbol": m["symbol"], "price": 1.0, "size": signed,
                     "commission": 0.0, "tag": m.get("tag", "")}) + "\n").encode())
                await writer.drain()
    return broker


def test_multilane_backfill_prior_day_does_not_lock_out_live():
    async def go():
        # ES: CURRENT session (2026-10-02), morning trades — goes live.
        es_msgs = [
            {"t": "bar", "ts": _ns_et(2026, 10, 2, 10, 0), "tf": "1m",
             "o": 5000, "h": 5001, "l": 4999, "c": 5000, "v": 5},
            {"t": "trade", "ts": _ns_et(2026, 10, 2, 10, 1),
             "price": 5000.0, "size": 1, "aggressor": 1},
            {"t": "trade", "ts": _ns_et(2026, 10, 2, 10, 2),
             "price": 5001.0, "size": 1, "aggressor": 1},
        ]
        # NQ: PRIOR session (2026-10-01) backfill bars ONLY, stamped 16:00 ET —
        # inside the flatten window. No current-day trade, so NQ stays warmup.
        nq_msgs = [
            {"t": "bar", "ts": _ns_et(2026, 10, 1, 16, 0), "tf": "1m",
             "o": 20000, "h": 20010, "l": 19990, "c": 20000, "v": 5},
            {"t": "bar", "ts": _ns_et(2026, 10, 1, 16, 1), "tf": "1m",
             "o": 20000, "h": 20010, "l": 19990, "c": 20005, "v": 5},
            {"t": "bar", "ts": _ns_et(2026, 10, 1, 16, 2), "tf": "1m",
             "o": 20005, "h": 20015, "l": 19995, "c": 20010, "v": 5},
        ]
        fills: dict[str, int] = {}
        msrv_es = await asyncio.start_server(_mk_market(es_msgs), "127.0.0.1", 0)
        msrv_nq = await asyncio.start_server(_mk_market(nq_msgs), "127.0.0.1", 0)
        bsrv = await asyncio.start_server(_mk_broker(fills), "127.0.0.1", 0)
        bport = bsrv.sockets[0].getsockname()[1]

        es_feed = NinjaTraderFeed("127.0.0.1", msrv_es.sockets[0].getsockname()[1], symbol="ES")
        nq_feed = NinjaTraderFeed("127.0.0.1", msrv_nq.sockets[0].getsockname()[1], symbol="NQ")
        es_broker = NinjaTraderBroker("127.0.0.1", bport, symbol="ES")
        nq_broker = NinjaTraderBroker("127.0.0.1", bport, symbol="NQ")
        s_es, s_nq = OnTrade("ES"), OnTrade("NQ")

        risk = RiskSupervisor(RiskConfig(eod_flatten_et=(15, 58)))
        eng = LiveEngine([es_feed, nq_feed], {"ES": es_broker, "NQ": nq_broker},
                         [s_es, s_nq], WallClock(), Blotter("ES", 50.0),
                         drain_timeout=0.3, warmup_gate=True, risk=risk)
        _t = asyncio.create_task(eng.run())
        await eng.wait_idle(timeout=10)
        eng._stop.set()
        await asyncio.wait_for(_t, timeout=10)
        for srv in (msrv_es, msrv_nq, bsrv):
            srv.close()
        return s_es, s_nq, eng, fills

    s_es, s_nq, eng, fills = asyncio.run(go())

    # 1. Lanes: ES live, NQ never left warmup (only prior-day backfill bars).
    assert "ES" in eng._live_lanes and "NQ" not in eng._live_lanes

    # 2. The tracked session day is the CURRENT day (10-02), not the backfill day.
    assert eng.risk._day == "2026-10-02", eng.risk._day

    # 3. THE REGRESSION: NQ's prior-day 16:00 backfill bars must NOT have set
    #    _eod_done for the active live session.
    assert eng.risk._eod_done is False, \
        "prior-day backfill must not lock out / pre-flatten the live session"

    # 4. ES's LIVE orders kept filling (nothing denied "post-EOD").
    assert fills.get("ES", 0) == 2, f"expected 2 ES live fills, got {fills}"
    assert s_es.trades == 2 and s_nq.trades == 0

    # 5. ES position is flat-or-long (it entered and was not force-flattened
    #    by a bogus prior-day EOD).
    assert eng.strategy_position(s_es) == 2
