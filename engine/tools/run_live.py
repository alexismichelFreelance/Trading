"""Run the LiveEngine against the NT8 EngineRelay sockets.

    # observation mode (default): consume the feed, no strategies, no orders
    .venv/Scripts/python.exe tools/run_live.py --seconds 60

    # trade on Sim101 with chosen sleeves (deployable causal configs)
    .venv/Scripts/python.exe tools/run_live.py --strategies opendrive,ignition --seconds 3600

Requires: NT8 running, connected, EngineRelay strategy ENABLED on an ES chart
(market socket 36001, broker socket 36002). Ctrl-C stops cleanly and prints the
blotter. Live ignition uses the CAUSAL config (trailing exit, no frozen states);
flow runs at 0.1x study size (maxp=5).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.brokers.ninjatrader import NinjaTraderBroker   # noqa: E402
from engine.adapters.feeds.ninjatrader import NinjaTraderFeed       # noqa: E402
from engine.adapters.feeds.recorder_tee import RecorderTee          # noqa: E402
from engine.adapters.painter import NTChartPainter                  # noqa: E402
from engine.adapters.questdb import AsyncQuestDB                     # noqa: E402
from engine.core.blotter import Blotter                             # noqa: E402
from engine.core.clock import WallClock                             # noqa: E402
from engine.core.config import load_instruments                     # noqa: E402
from engine.core.events import Bar, BookFlow, Trade                 # noqa: E402
from engine.core.live_engine import LiveEngine                      # noqa: E402
from engine.core.risk import RiskConfig, RiskSupervisor             # noqa: E402
from engine.painters import PaintController                         # noqa: E402

# Module logger. _spec() and build_roster() both call log.*; without this they
# raise NameError at the exact moment they are trying to report a problem.
log = logging.getLogger("engine.runner")

# How often the engine re-asks whether its gamma snapshot can answer for today.
# Cheap: pure snapshot arithmetic unless the answer is no.
GAMMA_CHECK_S = 300

# Minimum distance from a gamma pocket edge for a CONTINUATION entry.
# Trend sleeves made +115/trip deep in a short pocket and lost -145/trip
# within this distance of the boundary (442 vs 105 trips, pinned replay).
POCKET_EDGE_PTS = 15.0

HMM_PATH = str(ROOT / "config" / "hmm_es_1h.json")
SYMBOL = "ES"
INSTRUMENTS = load_instruments(ROOT / "config" / "instruments.yaml")


def fetch_daily_bars(symbol: str = "ES=F", days: int = 320) -> list:
    """Daily bars for the 1d zone view (Yahoo continuous front ~ current ES,
    small basis). Returns [] on any failure so painting degrades gracefully."""
    import time

    import httpx

    from engine.core.events import Bar
    try:
        p1 = int(time.time()) - days * 86400
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?period1={p1}&period2={int(time.time())}&interval=1d")
        r = httpx.get(url, timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
        out = []
        for i, t in enumerate(ts):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if None in (o, h, l, c):
                continue
            out.append(Bar((t + 21 * 3600) * 1_000_000_000, "1d", o, h, l, c,
                           int(q["volume"][i] or 0)))
        return out
    except Exception as ex:                       # noqa: BLE001
        print(f"  (daily-zone fetch failed: {ex}; 1d zones disabled)")
        return []


_DAILY_CACHE: dict = {}
_SESSION_DAILY_CACHE: dict = {}


def session_daily_bars(sym: str) -> list:
    """Daily bars of the TRADED contract on the ET CALENDAR DAY -- the session
    NT8's Pivots indicator uses on the user's chart (verified: chart PP 7779.44
    vs 7779.42 for 00:00-24:00 ET, against 7778.92 for Globex and 7778.83 for
    RTH on 2026-08-11).

    The pivot grid used to be seeded from Yahoo ES=F -- a CONTINUOUS series, in
    24-hour bars. Both are wrong for floor pivots: wrong instrument, wrong
    session. On 2026-08-07 Yahoo's daily low was 18 points below the real RTH
    low, putting PP 7 points and S1 14 points off, and a pivot entry fired with
    price nowhere near a level.

    Returns [] on any failure; the caller falls back to Yahoo and says so."""
    if sym in _SESSION_DAILY_CACHE:
        return _SESSION_DAILY_CACHE[sym]
    out = []
    try:
        import pandas as pd

        from engine.adapters.questdb import QuestDB
        from engine.core.events import Bar
        # Aggregate the RTH MINUTES explicitly. -- aggregated in pandas, not SAMPLE BY 1d WITH OFFSET '09:30'
        # buckets 09:30 -> 09:30, which is still a 24-hour window carrying the
        # whole overnight session -- the exact thing this function exists to
        # exclude. It happened to give the right H/L on 2026-08-07 and the wrong
        # close (7777.25 against the true 7783.25), which is how a bug like this
        # survives a spot check.
        df = QuestDB(timeout=90).df(
            f"SELECT ts, h, l, c, vol FROM claude_bars_live "
            f"WHERE symbol = '{sym}' ORDER BY ts")
        if not len(df):
            return []
        et = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("America/New_York")
        mod = et.dt.hour * 60 + et.dt.minute
        df = df.copy()
        df["day"] = et.dt.strftime("%Y-%m-%d")
        for day, g in df.groupby("day", sort=True):
            if len(g) < 300:                                  # partial session
                continue
            ts = int(pd.Timestamp(f"{day} 23:59", tz="America/New_York").value)
            out.append(Bar(ts, "1d", float(g["c"].iloc[0]), float(g["h"].max()),
                           float(g["l"].min()), float(g["c"].iloc[-1]),
                           int(g["vol"].sum() or 0), sym))
    except Exception as ex:                       # noqa: BLE001
        print(f"  (RTH daily aggregation failed for {sym}: {ex})")
        return []
    _SESSION_DAILY_CACHE[sym] = out
    return out


def daily_bars_for(sym: str) -> list:
    """~320 daily bars for `sym` (Yahoo continuous), fetched once and cached —
    used to seed both the 1d zone view and the pivot sleeve's D/W/M grid."""
    if sym not in _DAILY_CACHE:
        yahoo = INSTRUMENTS[sym].extra.get("yahoo", "ES=F" if sym == "ES" else None)
        _DAILY_CACHE[sym] = fetch_daily_bars(yahoo) if yahoo else []
    return _DAILY_CACHE[sym]


class ObserveStrategy:
    """Counts events; places no orders. Used to validate the live pipe.
    symbol='' = observer role: sees EVERY lane's events (dispatch broadcast)."""
    symbol = ""

    def __init__(self) -> None:
        self.trades = self.flows = self.bars = 0
        self.last_px = None

    def on_trade(self, e):
        self.trades += 1
        self.last_px = e.price
        return []

    def on_bookflow(self, e):
        self.flows += 1
        return []

    def on_bar(self, e):
        self.bars += 1
        self.last_px = e.c
        return []

    def on_quote(self, e):
        return []

    def on_depth(self, e):
        return []

    def on_fill(self, e):
        print(f"  FILL {e}")

    def on_position(self, e):
        print(f"  POSITION {e.qty} @ {e.avg_px}")


_GAMMA_CACHE: dict = {}


def _gamma_or_none(symbol: str):
    """SPX dealer-gamma regime for the *_gex variants. ES only — SPX gamma is
    not an NQ/GC signal. Fail-open: no GEX table / busy DB -> None (variant
    trades raw). One shared instance; short timeout so a busy QuestDB never
    stalls session startup."""
    # root_symbol, not a literal match: 'ESM5' and 'ESH5' ARE ES, and matching
    # the bare string silently gave them NO gate. Every 2025 replay therefore ran
    # its *_gex twins byte-identical to the raw ones -- which is why the gamma
    # result could not be validated out-of-sample: the wire was never connected.
    from engine.core.config import root_symbol
    if root_symbol(symbol) != "ES":
        return None
    if "ES" not in _GAMMA_CACHE:
        from engine.adapters.questdb import QuestDB
        from engine.features.gamma import GammaRegime
        try:
            _GAMMA_CACHE["ES"] = GammaRegime(QuestDB(timeout=30))
        except Exception as ex:      # noqa: BLE001
            print(f"  (*_gex variants trade RAW: GammaRegime unavailable: {ex})")
            _GAMMA_CACHE["ES"] = None
    return _GAMMA_CACHE["ES"]


def _spec(symbol: str):
    """InstrumentSpec for `symbol`, resolving contract months to their root
    ('ESM5' -> ES). Returns a stub with an empty `extra` when the instrument is
    not in the registry, so a missing entry falls back to the coded default
    rather than taking the engine down at startup."""
    from engine.core.config import root_symbol
    spec = INSTRUMENTS.get(root_symbol(symbol))
    if spec is None:
        log.warning("no instrument spec for %s; using coded defaults", symbol)
        return type("_Stub", (), {"extra": {}})()
    return spec


def _make(label: str, flow_th: int = 30, symbol: str = SYMBOL,
          hmm_path: str = HMM_PATH):
    """One strategy instance by roster label (includes every variant).
    Gamma-regime awareness is a STRATEGY choice (the *_gex variants), not an
    engine gate: raw and gamma-aware variants paper-trade side by side and the
    user picks which routes live."""
    from engine.strategies.dip_buy import DipBuyStrategy
    from engine.strategies.flow import FlowFollowingStrategy
    from engine.strategies.ibs_swing import IBSSwingStrategy
    from engine.strategies.ignition import IgnitionStrategy
    from engine.core.exits import TwoPhaseExit
    from engine.strategies.open_drive import OpenDriveStrategy
    from engine.strategies.overnight_break import OvernightBreakStrategy
    from engine.strategies.pivot import PivotStrategy
    from engine.strategies.sweep_follow import SweepFollowStrategy
    from engine.strategies.vwap_break import VwapBreakStrategy
    from engine.strategies.zones_strategy import ZoneLifecycleStrategy
    if label == "ignition":
        return IgnitionStrategy(symbol, hmm_path, exit_mode="trailing", regime_states=None)
    if label == "ignition_fixed":    # variant: fixed/regime exits instead of trailing
        return IgnitionStrategy(symbol, hmm_path, exit_mode="fixed", regime_states=None)
    if label == "opendrive":
        # ORB, not the blind 10:00 entry: every window shape loses on the blind
        # rule and the CLEAN drives lose worst (see tests/test_roster_decisions).
        return OpenDriveStrategy(symbol, mode="orb")
    if label == "opendrive_vac":
        # the twin of `opendrive`, differing ONLY in the break-quality gate, so
        # the forward record measures the gate and nothing else. See
        # engine/features/break_quality.py for the evidence and the fail-open.
        from engine.features.break_quality import BreakQuality
        return OpenDriveStrategy(symbol, mode="orb", vac_gate=BreakQuality())
    if label == "opendrive_2p":
        return OpenDriveStrategy(symbol, mode="orb",
                                 two_phase=TwoPhaseExit(12.0, "decay", 0.1))
    if label == "opendrive_2p_range":
        return OpenDriveStrategy(symbol, mode="orb",
                                 two_phase=TwoPhaseExit(12.0, "range", 0.25))
    if label == "opendrive_2p_retrace":   # strongest OOS variant
        return OpenDriveStrategy(symbol, mode="orb", two_phase=TwoPhaseExit(12.0, "retrace", 0.25))
    if label == "opendrive_2p16":
        return OpenDriveStrategy(symbol, mode="orb", two_phase=TwoPhaseExit(
            rev_kind="range", rev_f=0.25, arm_pts=16.0))
    if label == "opendrive_2p24":
        return OpenDriveStrategy(symbol, mode="orb", two_phase=TwoPhaseExit(
            rev_kind="range", rev_f=0.25, arm_pts=24.0))
    if label == "opendrive_2p32":
        return OpenDriveStrategy(symbol, mode="orb", two_phase=TwoPhaseExit(
            rev_kind="range", rev_f=0.25, arm_pts=32.0))
    if label == "flow":              # adaptive z-score threshold (scale-invariant)
        return FlowFollowingStrategy(symbol, maxp=5, adaptive=True, adapt_k=FLOW_K,
                                     gate_utc=FLOW_GATE)
    if label == "flow_fixed":        # variant: fixed threshold (research default)
        # KNOWN-RARE (audit: 2 orders / 34 sessions) and NOT a threshold bug:
        # th=30 already sits at p95 of live |adelta| (p50=4, p95=29, p99=59), so
        # it is correctly scaled to this feed. Together with the flat k=1..3
        # plateau in tools/flow_calib.py this says the threshold is NOT the
        # binding gate -- the other entry conditions (trend_lag / book / hold
        # bands) are. Retuning th or adapt_k cannot fix that; diagnosing which
        # condition starves the sleeve is its own piece of work.
        return FlowFollowingStrategy(symbol, maxp=5, adaptive=False, th=flow_th)
    pu = INSTRUMENTS[symbol].point_usd if symbol in INSTRUMENTS else 50.0
    if label == "zones":
        # BREAK stays off; the RUNNER is back on. Both were cut in 3f42771 on
        # numbers produced by the broken fill model (stops priced at the bar
        # close, entries at the market while every level was measured from the
        # zone edge). Re-derived from corrected fills over 23 sessions:
        #     break   14 trips  -2,112  50% win   (was -1,938 / 25%)
        #     runner  11 scaled trades: half off at +4 +7,262, RUNNER LEG +1,850
        # The runner was cut for "gives back more than it makes" (-2,100). It
        # makes +1,850. Break still loses, so it stays off -- but it is a small
        # loss at a coin-flip win rate, not the 1-in-4 outlier it was sold as.
        return ZoneLifecycleStrategy(symbol, point_usd=pu, enable_break=False)
    # timeframe sweep: 30m is the ES-validated default; these exist to find out
    # whether NQ wants a different bucket, not to be traded on faith.
    if label == "zones_15m":
        return ZoneLifecycleStrategy(symbol, point_usd=pu, tf="15m",
                                     enable_break=False)
    if label == "zones_1h":
        return ZoneLifecycleStrategy(symbol, point_usd=pu, tf="1h",
                                     enable_break=False)
    if label == "zones_4h":
        return ZoneLifecycleStrategy(symbol, point_usd=pu, tf="4h",
                                     enable_break=False)
    if label == "zones_gap":         # variant: leave-and-return gap zones on
        return ZoneLifecycleStrategy(symbol, gap_thr=5.0, point_usd=pu)
    if label == "dipbuy":
        return DipBuyStrategy(symbol, point_usd=pu)
    if label == "dipbuy_gex":        # variant: entries only on mid/long-gamma days
        return DipBuyStrategy(symbol, point_usd=pu, gamma=_gamma_or_none(symbol))
    if label == "ibs":
        return IBSSwingStrategy(symbol)
    if label == "ibs_gex":           # variant: size up in long-gamma
        return IBSSwingStrategy(symbol, gamma=_gamma_or_none(symbol))
    if label == "pivot":             # user-modeled: overnight bias + pivot fades
        return PivotStrategy(symbol, point_usd=pu, gamma=_gamma_or_none(symbol))
    if label == "vwapbreak":         # enter AT the line; chasing the break loses
        return VwapBreakStrategy(symbol, entry_mode="retest")
    # NINE vwapbreak variants REMOVED 2026-08-16: qual, qual_tol, qual_2p,
    # q_noext, q_noclean, q_band, q_bandnoext, retest_2p, 2p24.
    #
    # Measured across FOUR independent windows -- ES 2026 (46 sessions), NQ 2026
    # (26), ESM5 2025 (51), ESH5 2025 (22). The family is negative in three of
    # the four on every single variant:
    #     vwapbreak_qual        0/4   -7,862
    #     vwapbreak_qual_tol    1/4  -21,285
    #     vwapbreak_q_noclean   1/4  -16,447
    #     vwapbreak_qual_2p     1/4  -11,470
    #     vwapbreak_q_bandnoext 1/4  -10,508
    #     vwapbreak_retest_2p   1/4  -10,348
    #     vwapbreak             1/4   -7,397
    #     vwapbreak_q_band      1/4  +20,979  <- ALL of it from one ESM5 window
    # Ten labels for one bet, and the bet loses. q_band's apparent profit is the
    # same single-window artifact that made opendrive_2p24 look like the best
    # sleeve on the board the day before.
    #
    # These included the ablation twins built to find which of the three
    # qualified-break conditions carried the value. The answer, across four
    # windows, is none of them: the conditions were being ranked against each
    # other inside a family that does not work.
    #
    # `vwapbreak` (entry AT the line) survives as a PAPER CONTROL so the idea
    # stays measurable if the rule is re-specified. What four windows disprove
    # is THIS IMPLEMENTATION, not the trade -- the manual version reads
    # confluence and context that none of these variants encode.
    if label == "opendrive_pk":      # POCKET-GATED twin of `opendrive`
        from engine.strategies.open_drive import OpenDriveStrategy as _OD
        s = _OD(symbol, mode="orb")
        s.pocket_min_edge = POCKET_EDGE_PTS
        return s
    # LOCAL-SIGN twins of the _gex trio. Same sleeves, same want="short" gate,
    # but the regime comes from the sign of cumulative gamma AT PRICE instead of
    # gexp -- a 252-day percentile of the AGGREGATE book. Over 25 sessions with
    # both available the two agreed 7 times (28%): gexp said LONG on 18 while
    # price sat in a short-gamma pocket on 23. The curve is also OUR data,
    # measured daily, rather than a second unmonitored external feed.
    if label in ("ignition_lg", "flow_lg", "onbreak_lg"):
        base = label[:-3]
        s = _make(base, flow_th, symbol, hmm_path)
        s.gamma = None                 # the curve replaces the percentile
        s._wants_curve = True          # attach_curves() gives it the curve
        if label == "onbreak_lg":
            # FLIPPED to want="long". Under want="short" this scored -3,800 on
            # 22 of 29 days (p=0.974 against a random cull) while the 7 days it
            # REJECTED made +5,488 (p=0.033). onbreak breaks the OVERNIGHT
            # range; classifying it as a continuation sleeve may simply have
            # pointed the gate the wrong way. Running forward to find out --
            # see tests/test_regime_want.py for why this is not yet a result.
            s.regime_want = "long"
        return s
    if label == "wallfade":          # fade gamma walls, LONG-gamma pockets only
        from engine.strategies.wall_fade import WallFadeStrategy
        return WallFadeStrategy(symbol, point_usd=pu)
    if label == "onbreak":           # study-motivated: overnight-range break
        return OvernightBreakStrategy(symbol)
    # onbreak_2p and onbreak_2p24 REMOVED: the same entry with a decay/range
    # two-phase exit differing only in arming distance. On 2026-08-14 all five
    # onbreak rows entered at 10:22 and the family booked +3,650 of a +4,665
    # day -- one bet counted five times.
    #
    # WHICH one survives was settled by the PAIRED daily difference, not by
    # correlation and not by a convention. Correlation (+0.92..+0.98) says they
    # are redundant; it cannot say which to keep, because it is high precisely
    # BECAUSE they are identical on 26 of 29 days. Paired, over 29 ES sessions:
    #     2p32 vs 2p24     differ on  3 days, 2p32 wins 3/3, mean +546, t=7.67
    #     2p32 vs 2p       differ on  6 days, wins 4/6,      mean  +58, t=0.17
    # so 2p24 is dominated -- it never wins on a day where they differ -- and 2p
    # is indistinguishable. Arming further out (32 vs 24) means not arming on
    # smaller moves, so winners run; that only bites on the few days with a move
    # big enough, and on every one of those it paid.
    if label == "onbreak_2p32":
        return OvernightBreakStrategy(symbol, two_phase=TwoPhaseExit(
            rev_kind="range", rev_f=0.25, arm_pts=32.0))
    if label == "onbreak_2p_retrace":
        return OvernightBreakStrategy(symbol,
                                      two_phase=TwoPhaseExit(12.0, "retrace", 0.25))
    if label == "sweepfade":         # MBO-derived: fade deep aggressive sweeps
        return SweepFollowStrategy(symbol, min_span_ticks=6, hold_s=15.0)
    if label == "sweepfade_deep":    # higher conviction, fewer signals
        return SweepFollowStrategy(symbol, min_span_ticks=8, hold_s=15.0)
    if label == "sweepfollow":       # measured LOSER; paper twin as the control
        return SweepFollowStrategy(symbol, min_span_ticks=6, hold_s=15.0,
                                   mode="follow")
    if label == "rsi2":              # Connors RSI(2), the IBS satellite
        from engine.strategies.rsi2_swing import RSI2SwingStrategy
        return RSI2SwingStrategy(symbol)
    if label.startswith("trendjoin"):
        from engine.core.exits import TwoPhaseExit
        from engine.strategies.trend_join import TrendJoinStrategy
        # Sizing comes from the instrument registry, not from ES numbers copied
        # onto NQ: 15pt is 23% of an ES range (no discrimination -- small legs
        # still reach it 97% of the time) and utterly meaningless on NQ, which
        # ranges ~7x wider. See config/instruments.yaml trend_join.
        tj = (_spec(symbol).extra.get("trend_join") or {})
        conf = float(tj.get("conf_pts", 27.0))
        stop = float(tj.get("stop_pts", 6.0))
        if label == "trendjoin":                 # clock exit -- the control
            return TrendJoinStrategy(symbol, conf_pts=conf, stop_pts=stop)
        if label == "trendjoin_pk":              # POCKET-GATED twin of the above
            s = TrendJoinStrategy(symbol, conf_pts=conf, stop_pts=stop)
            s.pocket_min_edge = POCKET_EDGE_PTS
            return s
        if label == "trendjoin_narrow":          # half the confirmation
            return TrendJoinStrategy(symbol, conf_pts=conf / 2, stop_pts=stop)
        if label == "trendjoin_fast":
            # Scales on ARRIVAL SPEED rather than on the day being spent: half
            # comes off when price is AT a session extreme in our favour having
            # travelled >=0.15 of a typical day's range in the last ten minutes.
            # That is the only reversal feature that replicated out of sample --
            # 4 of 4 cells across 2025 Databento MBO and the 2026 live record,
            # tops and bottoms, ~2x separation -- and the only one computable
            # from price alone, so the live feed can actually produce it.
            s = TrendJoinStrategy(symbol, conf_pts=conf / 2, stop_pts=stop, qty=2)
            s.wants_day_range = True
            s.scale_push = 0.15
            return s
        if label == "trendjoin_scale80w":
            # scale80 WITH the one-way suppressor. The pair isolates it: same
            # entry, same exits, same 0.80 level -- the only difference is that
            # this one declines to scale on a day that has built its range
            # without a single 25% pullback. All four 2026 sessions where the
            # bare trigger fired disastrously early had zero counter-moves.
            s = TrendJoinStrategy(symbol, conf_pts=conf / 2, stop_pts=stop, qty=2)
            s.wants_day_range = True
            s.scale_at = 0.8
            s.skip_one_way = True
            return s
        if label in ("trendjoin_scale80", "trendjoin_scale100"):
            # The SCALE-OUT twins of trendjoin_narrow: same entry, same exits,
            # two lots instead of one, and half comes off once the session has
            # produced `scale_at` of a typical day's range. Two levels run
            # because the right one is not known -- 0.86 was where 2026-08-21
            # topped on ES and 1.06 on NQ, so the answer is bracketed rather
            # than fitted. The forward record chooses.
            s = TrendJoinStrategy(symbol, conf_pts=conf / 2, stop_pts=stop, qty=2)
            s.wants_day_range = True
            s.scale_at = 0.8 if label.endswith("80") else 1.0
            return s
        if label in ("trendjoin_2p24", "trendjoin_2p32"):
            # arming distance in the SAME units as conf, scaled per instrument
            f = 0.9 if label.endswith("24") else 1.2
            return TrendJoinStrategy(symbol, conf_pts=conf, stop_pts=stop,
                                     two_phase=TwoPhaseExit(
                                         rev_kind="retrace", rev_f=0.25,
                                         arm_pts=conf * f))
        raise SystemExit(f"unknown trendjoin variant '{label}'")

    raise SystemExit(f"unknown strategy '{label}'")


# Adaptive-flow threshold: th = rolling_mean(|adelta|) + FLOW_K * rolling_std.
# Was 4.0, which fired ZERO orders in 34 captured sessions (tools/sleeve_audit.py
# verdict DEAD). tools/flow_calib.py measured the real fire rate per k over that
# capture: k=1.0..3.0 all sit on a FLAT plateau (~1-2 entries/day, 23.5% of
# sessions) and it collapses to 0 at k=4.0 -- i.e. below k~3 the threshold is not
# even the binding gate, and 4.0 sat just past a cliff. 2.0 is the middle of the
# plateau (robust to the exact value) and the conventional 2-sigma.
# Re-run tools/flow_calib.py before changing this.
FLOW_K = 2.0

# Flow's trading window, UTC hours. The CLASS default is (13, 21) = 09:00-17:00
# ET, which is the research window flow_oracle.py is parity-checked against --
# so it is NOT changed there. LIVE overrides it to close at 16:00 ET instead.
#
# Why: on 2026-07-27 flow entered at 09:40 ET and its only exit was the 17:00 ET
# window edge. The CME session ends 17:00 ET, so the flatten could not fire
# until the feed resumed after the maintenance halt -- it went through at
# 17:30:55 ET, meaning the position was carried across the settlement break with
# no way to act on it. Closing at 16:00 ET keeps flow inside the session it was
# measured in and inside RTH, where every other sleeve flattens.
FLOW_GATE = (13, 20)          # 09:00 -> 16:00 ET

# every strategy + variant — the full paper roster (--paper all)
ALL_LABELS = ("ignition", "ignition_fixed", "opendrive",
              "flow", "flow_fixed",
              "zones", "zones_gap", "ibs", 
              "pivot", "vwapbreak", "onbreak",               "trendjoin_pk", "opendrive_pk", "wallfade",
              "ignition_lg", "flow_lg", "onbreak_lg",
              "opendrive_vac",          # break-quality twin of `opendrive`
              "trendjoin_scale80", "trendjoin_scale100",   # scale-out twins
              "trendjoin_fast",         # scales on ARRIVAL SPEED instead
              "trendjoin_scale80w",     # scale80 + one-way suppressor
              "rsi2", "trendjoin", "trendjoin_narrow",
              "trendjoin_2p24", "trendjoin_2p32",
                            "onbreak_2p_retrace",
                            # fixed-distance arming twins — the variant that measured better
              # than the volatility ruler; 16/24/32pt all run, none privileged
              "opendrive_2p24",
              "onbreak_2p32",
              # NINE vwapbreak variants REMOVED 2026-08-16 -- see _make().
              # `vwapbreak` alone stays, as a paper control.
              "zones_15m", "zones_1h", "zones_4h")


def lane_gamma_levels(sym: str, day: str, gex_basis_override=None):
    """(lv, underlying, basis) — prior-session gamma walls for a lane's
    instrument on `day`, from its configured underlying+basis. lv is None if the
    lane has no gex config or no prior row. FRESH DB read each call, so a
    long-running engine picks up rows written since startup (the 15:00 fetch)."""
    gexc = INSTRUMENTS[sym].extra.get("gex")
    if not gexc:
        return None, None, None
    from engine.adapters.questdb import QuestDB as _Q
    from engine.features.gamma_levels import GammaLevels
    und = gexc.get("underlying", "SPX")
    q = _Q(timeout=30)
    # MEASURED, not configured. The yaml constants (ES 42.0, NQ 181.0) were each
    # a single reading on 2026-07-20 with a comment saying to re-measure, and
    # nothing did -- a comment is not a mechanism. The basis DECAYS as the front
    # contract approaches expiry: ES 47.6 -> 22.5 and NQ 225.1 -> 99.5 over 13
    # stored sessions. By 2026-08-15 the configured values were 21.7 (ES) and
    # 81.5 (NQ) points out, so every wall and flip was drawn that far from where
    # it belongs, worsening daily until the roll. A constant is the wrong SHAPE
    # for the quantity. The yaml value is now only a fallback for when there is
    # nothing to measure, and it says so when it is used.
    if sym == "ES" and gex_basis_override is not None:
        basis = gex_basis_override
    else:
        from engine.features.gamma_basis import BasisSeries
        basis = BasisSeries.load(q, und, sym,
                                 float(gexc.get("basis", 0.0))).for_day(day)
    lv = GammaLevels(q, underlying=und).levels_prev(day, basis=basis)
    return lv, und, basis


def session_close_px(qdb, symbol: str, last_ts: int, now_ns: int | None = None):
    """Closing price of the ET session `last_ts` falls in, IF that session has
    already ended. None otherwise (same session -> the engine uses the live mark).

    This exists so an orphaned INTRADAY position is booked out where the engine's
    own 15:59 session flat would have taken it. The engine died at 11:10 ET on
    2026-08-04 holding 21 of them and restarted the next morning; marking those
    at the next day's price charges each sleeve a full overnight session it was
    never, by design, exposed to.

    Any failure returns None -- a missing price is recoverable (the engine books
    the position out flat at its entry), a wrong price is not, and neither is
    taking startup down over it."""
    from engine.core.timeutil import et_session_date
    import pandas as _pd
    try:
        now_ns = now_ns if now_ns is not None else _pd.Timestamp.now("UTC").value
        if et_session_date(last_ts) == et_session_date(now_ns):
            return None                              # still the same session
        day = et_session_date(last_ts)
        lo = _pd.Timestamp(f"{day} 00:00", tz="America/New_York").tz_convert("UTC")
        hi = _pd.Timestamp(f"{day} 16:00", tz="America/New_York").tz_convert("UTC")
        b = qdb.df(f"SELECT ts, c FROM claude_bars_live WHERE symbol='{symbol}' "
                   f"AND ts >= '{lo.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
                   f"AND ts <= '{hi.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
                   f"ORDER BY ts")
        if b is None or not len(b):
            return None                              # no bar for that day
        return float(b["c"].iloc[-1])
    except Exception:                                # noqa: BLE001
        return None


def load_open_paper_positions(qdb, symbols) -> dict:
    """{sleeve_label: (pos, avg_px, last_fill_ts_ns)} for paper sleeves currently
    holding an open position, avg-cost-reconstructed from the full
    claude_paper_fills history (closed round-trips net to zero and drop out).
    Empty on any read failure.

    The fill TIME is carried because the engine cannot otherwise tell a restart
    inside the same session from one across a gap, and the two need different
    close prices for an unrestorable position (see session_close_px)."""
    out: dict = {}
    for sym in symbols:
        try:
            pf = qdb.df(f"SELECT ts, sleeve, side, qty, price FROM claude_paper_fills "
                        f"WHERE symbol='{sym}' ORDER BY ts")
        except Exception:                            # noqa: BLE001
            continue
        if not len(pf):
            continue
        for sl in pf["sleeve"].unique():
            g = pf[pf["sleeve"] == sl]
            pos = 0
            avg = 0.0
            for r in g.itertuples():
                q = int(r.qty) if r.side > 0 else -int(r.qty)
                px = float(r.price)
                if pos == 0:
                    pos, avg = q, px
                elif (q > 0) == (pos > 0):            # add: weighted average
                    avg = (avg * abs(pos) + px * abs(q)) / (abs(pos) + abs(q))
                    pos += q
                elif abs(q) >= abs(pos):             # close through flat / flip
                    pos += q
                    avg = px if pos != 0 else 0.0
                else:                                # partial reduce: avg unchanged
                    pos += q
            if pos != 0:
                import pandas as pd
                last_ts = int(pd.Timestamp(g["ts"].iloc[-1]).value)
                out[str(sl)] = (pos, avg, last_ts)
    return out


def attach_day_range(sleeves, symbol: str, shared=None):
    """Give every sleeve that wants it the SAME DayRange for this lane.

    market data -> indicators -> strategies. DayRange is an indicator: it turns
    the tape into "how much of a normal day has happened", and strategies decide
    what that means for them. It does not belong in the engine, which routes
    orders and owns positions, and it does not belong to one sleeve, because the
    day being spent is a fact about the DAY.

    One instance per lane, exactly like attach_curves does for the gamma curve.
    Each sleeve feeds it from its own dispatch; DayRange.note is idempotent on
    timestamp so the same event arriving once per sleeve updates it once.

    Fail-open: a sleeve that never receives one keeps day_range=None, and every
    consumer treats that as "no opinion"."""
    want = [s for s in sleeves if getattr(s, "wants_day_range", False)]
    if not want:
        return None
    from engine.features.day_range import DayRange
    dr = shared if shared is not None else DayRange()
    for s in want:
        s.day_range = dr
    log.info("day-range indicator attached to %d sleeve(s) for %s",
             len(want), symbol)
    return dr


def attach_curves(sleeves, symbol: str, day: str, curve=None) -> None:
    """Give every curve-wanting sleeve the PRIOR session's gamma curve.

    _wants_curve was set on the twins and read only by portfolio_replay, so live
    they ran with pocket=None -- no gate, identical to their originals, and any
    "evidence" they accumulated was meaningless. The same wire-never-connected
    failure as the 2025 _gex twins.

    Fail-open: on any error the sleeve keeps its existing pocket (None), which
    gamma_entry_ok and pocket_entry_ok both treat as no filter."""
    want = [s for s in sleeves
            if getattr(s, "_wants_curve", False)
            or getattr(s, "pocket_min_edge", 0.0) > 0.0
            or type(s).__name__ == "WallFadeStrategy"]
    if not want:
        return
    if curve is None:
        try:
            from engine.adapters.questdb import QuestDB
            from engine.core.config import root_symbol
            from engine.features.gamma_basis import BasisSeries
            from engine.features.gamma_curve import GammaCurve
            root = root_symbol(symbol)
            und = {"ES": "SPX", "NQ": "NDX"}.get(root)
            if und is None:
                return
            q = QuestDB(timeout=30)
            basis = BasisSeries.load(q, und, root, 0.0).for_day(day)
            c = GammaCurve.load_prev(q, day, basis, underlying=und)
            curve = c if c.ok else None
        except Exception as ex:                        # noqa: BLE001
            log.warning("no gamma curve for %s on %s (%s); the curve-gated "
                        "sleeves trade UNGATED today", symbol, day, ex)
            return
    for s in want:
        s.pocket = curve
    log.info("gamma curve attached to %d sleeve(s) for %s %s (sess %s)",
             len(want), symbol, day, getattr(curve, "sess", "-") if curve else "none")


def build_roster(paper: str, flow_th: int = 30, symbol: str = SYMBOL,
                 hmm_path: str = HMM_PATH, prefix: str = ""):
    """Roster for one instrument lane. `prefix` namespaces labels in multi-
    instrument mode ('NQ:zones'); empty in the single-instrument default."""
    labels = list(ALL_LABELS) if paper.strip() in ("all", "") else \
        [x for x in paper.split(",") if x]
    roster = []
    dropped = []
    for lb in labels:
        # A stale name must cost its own sleeve and nothing else. On 2026-08-12
        # 'opendrive_orb' -- removed from _make the day before as a duplicate --
        # was still listed in config/live.yaml, and _make's SystemExit took the
        # whole engine down at the open. Nine working sleeves stood down for a
        # tenth that no longer existed. Same rule as dispatch isolation: the
        # blast radius of a broken sleeve is that sleeve.
        try:
            s = _make(lb, flow_th, symbol, hmm_path)
        except BaseException as ex:                  # SystemExit included
            dropped.append(lb)
            log.error("ROSTER: dropping unknown sleeve %r for %s (%s) -- the rest "
                      "of the book still trades", lb, symbol, ex)
            continue
        s.label = prefix + lb        # for display / paper reporting
        roster.append((prefix + lb, s))
    if dropped and not roster:
        raise SystemExit(f"no sleeves could be built for {symbol}: {dropped}")
    return roster


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed-delay-s", type=float, default=600.0,
                    help="seconds of delay the DATA SUBSCRIPTION imposes (this "
                         "account is on a 10-minute delayed CME feed, so 600). "
                         "Lag reporting alarms on the excess over this, never "
                         "on the baseline itself. 0 for a real-time feed.")
    ap.add_argument("--paper", default="all",
                    help="roster that ALWAYS paper-trades (visible signals): 'all' (every "
                         "strategy + variant) or a comma list of labels. Default 'all'.")
    ap.add_argument("--live", default="",
                    help="comma list of roster labels that ALSO route to the NT8 broker "
                         "(the few that trade live). Empty = pure paper/observation.")
    ap.add_argument("--strategies", default="",
                    help="DEPRECATED alias for --live (kept for old commands)")
    ap.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = run until Ctrl-C)")
    ap.add_argument("--instruments", default="",
                    help="subset of instrument lanes to run (e.g. ES or ES,NQ). "
                         "DEFAULT (empty) = every lane in --live-config (config/live.yaml).")
    ap.add_argument("--live-config", default=str(ROOT / "config" / "live.yaml"),
                    help="per-instrument lane config (ports, paper/live rosters)")
    ap.add_argument("--market-port", type=int, default=36001)
    ap.add_argument("--broker-port", type=int, default=36002)
    ap.add_argument("--draw-port", type=int, default=36004,
                    help="EngineOverlay INDICATOR's draw socket (drawing lives in an "
                         "indicator, on the chart, so it never hides your orders)")
    ap.add_argument("--no-warmup-gate", action="store_true",
                    help="DANGER: let strategies trade on backfill bars (debug only)")
    ap.add_argument("--no-paint", action="store_true",
                    help="disable NT8 chart drawing (ghost signals, zones, status box)")
    ap.add_argument("--paper-paint", action="store_true",
                    help="also PAINT paper-sleeve fills (cyan). Default OFF: 10 paper "
                         "sleeves add many chart objects; they're always recorded to "
                         "claude_paper_fills regardless (review via tools/scorecard.py)")
    ap.add_argument("--panel", default="bottomleft",
                    choices=["bottomleft", "topright", "bottomright", "topleft"],
                    help="corner for the engine info panel (default bottomleft)")
    ap.add_argument("--record", action=argparse.BooleanOptionalAction, default=True,
                    help="record 1m bars + per-second features to QuestDB (default ON; "
                         "--no-record to disable)")
    ap.add_argument("--flow-th", type=int, default=30,
                    help="flow threshold for THIS feed (validated=200 but the live feed's "
                         "|adelta| tops ~66; default 30 lets flow respond here)")
    ap.add_argument("--max-sleeve", type=int, default=10,
                    help="risk: cap on any sleeve's |position| (default 10)")
    ap.add_argument("--max-gross", type=int, default=15,
                    help="risk: cap on account gross exposure across sleeves (default 15)")
    ap.add_argument("--max-sleeve-usd", type=float, default=None,
                    help="risk: DOLLAR-notional cap per sleeve (contracts x px x $/pt); "
                         "comparable across instruments. Off by default.")
    ap.add_argument("--max-gross-usd", type=float, default=None,
                    help="risk: DOLLAR-notional cap on account gross. Off by default.")
    ap.add_argument("--risk-halt", type=float, default=-5000.0,
                    help="risk: daily marked-loss kill switch in USD (default -5000)")
    ap.add_argument("--no-risk", action="store_true",
                    help="DANGER: disable production risk limits (in-flight vetting stays on)")
    ap.add_argument("--raw-capture", action=argparse.BooleanOptionalAction, default=True,
                    help="record the FULL raw feed (every trade + all 10 L2 book levels) "
                         "to claude_ticks_live/claude_depth_live via a buffered ILP writer "
                         "thread, off the hot path (default ON; --no-raw-capture to disable)")
    ap.add_argument("--gex-levels", action=argparse.BooleanOptionalAction, default=True,
                    help="draw prior-session gamma walls (put/call/flip) as S/R lines "
                         "(default ON; --no-gex-levels to disable)")
    ap.add_argument("--gex-basis", type=float, default=None,
                    help="override the ES lane's cash->future basis (default: "
                         "instruments.yaml gex.basis, re-measured at each close)")
    a = ap.parse_args()

    # Console AND a durable file. Until 2026-08-05 this was console-only, so when
    # the engine stopped trading at 11:10 ET the counters, the stack trace and
    # every diagnostic died with the terminal -- the failure could only be
    # inferred from gaps in QuestDB tables hours later. A log you cannot read
    # after the fact is not a log. Same lesson as the GEX task in July, which
    # buffered stdout into a file and lost everything when Windows killed it.
    _logdir = ROOT / "logs"
    _logdir.mkdir(exist_ok=True)
    _fh = logging.handlers.TimedRotatingFileHandler(
        _logdir / "engine.log", when="midnight", backupCount=30,
        encoding="utf-8", delay=False)
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    _ch = logging.StreamHandler()
    _ch.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s",
                                       datefmt="%H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[_ch, _fh], force=True)
    logging.getLogger("engine").info(
        "engine starting -- log file %s", _logdir / "engine.log")
    # silence per-request HTTP logs (QuestDB recorder calls via httpx) — pure noise
    for noisy in ("httpx", "httpcore", "hpack", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # ── instrument lanes ──────────────────────────────────────────────────
    # config/live.yaml is the standing config. The BARE command runs every lane
    # defined there (ES + NQ today) — no flags needed. --instruments ES,NQ picks
    # a subset. If live.yaml is absent, fall back to one ES lane from the flat
    # CLI args (ports/paper/live).
    cfg_path = Path(a.live_config)
    if cfg_path.exists():
        import yaml as _yaml
        _all = (_yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}).get("instruments") or {}
        if not _all:
            raise SystemExit(f"no instruments defined in {a.live_config}")
        want = [s.strip() for s in a.instruments.split(",") if s.strip()] or list(_all)
        lanes_cfg = {}
        for sym in want:
            if sym not in _all:
                raise SystemExit(f"instrument {sym}: no entry in {a.live_config} "
                                 f"(have {sorted(_all)})")
            lanes_cfg[sym] = _all[sym]
        multi = True                                 # live.yaml labels stay prefixed (ES:zones)
    else:
        lanes_cfg = {SYMBOL: {"market_port": a.market_port, "broker_port": a.broker_port,
                              "draw_port": a.draw_port, "paper": a.paper,
                              "live": (a.live or a.strategies)}}
        multi = False

    roster: list = []            # (label, strategy) across all lanes
    live_names: set = set()
    feeds: list = []
    brokers: dict = {}
    recorders: list = []
    rawcaps: list = []
    lane_meta: dict = {}         # sym -> {draw_port, roster, lc}
    blot: Blotter | None = None
    for sym, lc in lanes_cfg.items():
        spec = INSTRUMENTS.get(sym)
        if spec is None:
            raise SystemExit(f"lane {sym}: no spec in config/instruments.yaml")
        prefix = f"{sym}:" if multi else ""
        hmm = str(ROOT / spec.hmm_path) if spec.hmm_path else HMM_PATH
        lane_roster = build_roster(str(lc.get("paper", "all")), flow_th=a.flow_th,
                                   symbol=sym, hmm_path=hmm, prefix=prefix)
        lane_live = {prefix + x for x in str(lc.get("live") or "").split(",") if x}
        unknown = lane_live - {lb for lb, _ in lane_roster}
        if unknown:
            raise SystemExit(f"--live names not in the {sym} roster: {sorted(unknown)}")
        # lane discipline: feed stamp == strategy symbols == broker key
        assert all(s.symbol == sym for _, s in lane_roster), f"symbol mismatch in {sym} lane"
        # ONE DayRange indicator per lane, shared by every sleeve that asks.
        # Attached here rather than in the painting block because an indicator
        # must exist whether or not anything is being drawn, and unlike the
        # gamma curve it needs no re-attaching at the session rollover -- it
        # rolls itself from the tape.
        attach_day_range([s for _, s in lane_roster], sym)
        # emit_depth only when raw-capturing (the engine itself needs BookFlow,
        # not raw depth — RawCaptureTee records the depth and drops it onward).
        feed = NinjaTraderFeed("127.0.0.1", int(lc.get("market_port", 36001)),
                               symbol=sym, emit_depth=a.raw_capture)
        if a.raw_capture:                            # innermost: sees raw depth first
            from engine.adapters.feeds.raw_capture import RawCaptureTee
            feed = RawCaptureTee(feed, symbol=sym)
            rawcaps.append(feed)
        if a.record:
            rec = RecorderTee(feed, AsyncQuestDB(), symbol=sym)
            recorders.append(rec)
            feed = rec
        feeds.append(feed)
        brokers[sym] = NinjaTraderBroker("127.0.0.1", int(lc.get("broker_port", 36002)),
                                         symbol=sym)
        if blot is None:
            blot = Blotter(sym, spec.point_usd, verbose=True)
        else:
            blot.add_instrument(sym, spec.point_usd)
        roster += lane_roster
        live_names |= lane_live
        lane_meta[sym] = {"draw_port": int(lc.get("draw_port", 36004)),
                          "roster": lane_roster, "lc": lc}

    # seed the pivot sleeve's day/week/month grid from daily-bar history so the
    # weekly/monthly pivots are correct on day one (not filled in over weeks).
    for sym, meta in lane_meta.items():
        pivs = [s for _, s in meta["roster"] if hasattr(s, "seed_history")]
        if pivs:
            # RTH aggregates of the traded contract; Yahoo (continuous, 24h) only
            # as a fallback, and never silently -- it puts the grid points out.
            hist = session_daily_bars(sym)
            src = "ET-calendar-day (traded contract)"
            if not hist:
                hist = daily_bars_for(sym)
                src = "YAHOO CONTINUOUS 24h -- pivot levels will be OFF by points"
            for st in pivs:
                st.seed_history(hist)
            if hist:
                print(f"seeded {len(hist)} bars -> {sym} pivot D/W/M grid [{src}]")

    paper_blot = None
    if a.record:
        from engine.adapters.paper_blotter import PaperBlotter
        paper_blot = PaperBlotter(AsyncQuestDB())
        await paper_blot.start()
        print("recording -> QuestDB: 1m bars (claude_bars_live) + per-second "
              "aggressor/book (claude_sec_live) + paper fills (claude_paper_fills)")
    obs = ObserveStrategy()
    strategies = [obs] + [s for _, s in roster]
    live_owners = {id(s) for lb, s in roster if lb in live_names}
    label_by_id = {id(s): lb for lb, s in roster}
    # production risk config (the 2026-07-09 burst fixes). IBS carries positions
    # overnight by design, so EOD flatten stays compatible only because IBS
    # isn't in the default live set; revisit eod_flatten_et before enabling it.
    point_usd = {sym: INSTRUMENTS[sym].point_usd for sym in lanes_cfg}
    if a.no_risk:
        risk = RiskSupervisor(RiskConfig(point_usd=point_usd))
        print("risk: PRODUCTION LIMITS OFF (--no-risk); in-flight vetting only")
    else:
        risk = RiskSupervisor(RiskConfig(
            point_usd=point_usd,
            max_pos_per_sleeve=a.max_sleeve, max_account_gross=a.max_gross,
            max_sleeve_notional_usd=a.max_sleeve_usd,
            max_gross_notional_usd=a.max_gross_usd,
            rate_max_orders=4, rate_window_s=5.0,
            daily_loss_halt=a.risk_halt,
            entry_lockout_et=(15, 45), eod_flatten_et=(15, 58),
            swing_sleeves=("IBSSwingStrategy",)))     # IBS enters 15:59 + holds overnight
        usd_caps = ""
        if a.max_sleeve_usd:
            usd_caps += f", sleeve ${a.max_sleeve_usd:,.0f}"
        if a.max_gross_usd:
            usd_caps += f", gross ${a.max_gross_usd:,.0f}"
        print(f"risk: sleeve cap {a.max_sleeve}, gross cap {a.max_gross}{usd_caps}, "
              f"4 orders/5s, halt at ${a.risk_halt:+,.0f}, "
              f"entry lockout 15:45 ET, EOD flatten 15:58 ET (IBS exempt: swing)")
    eng = LiveEngine(feeds, brokers, strategies, WallClock(), blot,
                     warmup_gate=not a.no_warmup_gate, risk=risk,
                     live_owners=live_owners)
    # THE one opt-in for absolute lag reporting: a live feed's timestamps are
    # wall-clock, so the difference means something here and nowhere else.
    # See LiveEngine._note_lag -- on 2026-08-12 the engine spent an hour behind
    # the tape and no number in the log said so.
    eng.lag_report_s = 60.0
    # ...and this account's data subscription is 10-minute DELAYED CME, so 600s
    # of lag is the permanent, correct baseline. Alarming on it produced 173
    # ERROR lines on 2026-08-13 for a condition that is just how the data
    # arrives. Only the EXCESS over this is a fault.
    eng.feed_delay_s = float(a.feed_delay_s)
    live_lbls = sorted(lb for lb, s in roster if id(s) in live_owners)
    paper_lbls = sorted(lb for lb, s in roster if id(s) not in live_owners)
    print(f"roster ({len(roster)}): LIVE->NT8 {live_lbls or '(none)'}  |  "
          f"PAPER {paper_lbls}")

    # resume open paper positions carried across a restart (e.g. IBS held
    # overnight) so they get managed/exited instead of orphaned. Rebuilt from
    # claude_paper_fills; applied at each lane's warmup->live flip.
    by_label = {lb: s for lb, s in roster}
    try:
        from engine.adapters.questdb import QuestDB as _RQDB
        open_pos = load_open_paper_positions(_RQDB(timeout=10), list(lanes_cfg))
    except Exception as ex:                          # noqa: BLE001
        open_pos = {}
        print(f"  (paper-position restore skipped: {ex})")
    for sl, (pos, avg, last_ts) in open_pos.items():
        s = by_label.get(sl)
        if s is not None and id(s) not in live_owners:
            # If the sleeve cannot resume, the engine books it out -- at that
            # session's close when the session has already ended, so an intraday
            # sleeve is not charged an overnight it never held.
            try:
                cpx = session_close_px(_RQDB(timeout=10), getattr(s, "symbol", ""),
                                       last_ts)
            except Exception:                        # noqa: BLE001
                cpx = None
            eng.restore_paper_position(s, pos, avg, close_px=cpx)
            # Say what will actually happen. This line used to promise a close
            # at `cpx`, which was the earlier design; the position is now VOIDED
            # at its entry for zero P&L, and cpx only reports what was abandoned.
            print(f"resuming paper position: {sl} {pos:+d} @ {avg:.2f} "
                  f"(at warmup->live flip"
                  + (f"; if unrestorable, VOIDED at entry for 0.00 — it would "
                     f"have been {(cpx - avg) * pos:+.1f} pts at {cpx:.2f}, "
                     f"not booked)" if cpx else "; if unrestorable, VOIDED at "
                     f"entry for 0.00)"))

    # ── chart painting: ONE painter + PaintController PER LANE (each lane's
    # EngineOverlay indicator has its own draw socket). Engine hooks fan out,
    # routed by the event/fill symbol so a lane only paints its own chart.
    painters: list[NTChartPainter] = []
    pcs: dict[str, PaintController] = {}          # symbol -> lane controller
    pc: PaintController | None = None             # primary lane (back-compat refs)
    if not a.no_paint:
        for sym, meta in lane_meta.items():
            ptr = NTChartPainter("127.0.0.1", meta["draw_port"])
            if not await ptr.connect():
                print(f"  ({sym}: no draw socket on {meta['draw_port']}; painting off)")
                continue
            painters.append(ptr)
            lane_strats = [s for _, s in meta["roster"]]
            lane_pc = PaintController(ptr, lane_strats, panel_pos=a.panel)
            pcs[sym] = lane_pc
            if pc is None:
                pc = lane_pc
            daily = daily_bars_for(sym)          # cached (also seeds pivot grid)
            if daily:
                lane_pc.zv.seed_daily(daily)
                print(f"seeded {len(daily)} daily bars for {sym} 1d zones")
            print(f"chart painting ON [{sym}] (zones, S/R, gamma, signals, panel)")
            # gamma S/R levels per lane: ES <- SPX chain, NQ <- NDX chain
            # (instruments.yaml gex: {underlying, basis}); --gex-basis still
            # overrides the ES basis for back-compat.
            if a.gex_levels and INSTRUMENTS[sym].extra.get("gex"):
                from datetime import datetime, timezone
                try:
                    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    lv, und, basis = lane_gamma_levels(sym, today, a.gex_basis)
                    # the CURVE for the curve-gated sleeves of this lane. Without
                    # this they run with pocket=None -- ungated, identical to
                    # their originals, and every session of "evidence" is void.
                    attach_curves([s for lb, s in roster if lb.startswith(f"{sym}:")
                                   or ":" not in lb], sym, today)
                    if lv:
                        lane_pc.set_gamma_levels(lv)
                        print(f"gamma S/R levels ON [{sym}<-{und}] ({lv['sess']}, "
                              f"+{basis:.0f} basis): "
                              f"putW {lv['put_wall']:.0f}  callW {lv['call_wall']:.0f}  "
                              f"flip {lv['flip'] and round(lv['flip'])}  "
                              f"{'long' if lv['net_sign']>0 else 'SHORT'}-gamma")
                        if basis == 0.0 and sym != "ES":
                            print(f"  ({sym}: basis UNCALIBRATED — eyeball wall vs price, "
                                  f"then set gex.basis = {sym}_close - {und}_close "
                                  f"in config/instruments.yaml)")
                    else:
                        print(f"  ({sym}: no prior {und} gamma-levels row; "
                              f"run tools/fetch_cboe_gex.py)")
                except Exception as ex:                  # noqa: BLE001
                    print(f"  ({sym} gamma levels disabled: {ex})")
        if pcs:
            def _pc_of(sym: str) -> PaintController | None:
                return pcs.get(sym) or (pc if len(pcs) == 1 else None)

            async def _live_fill(f):
                p = _pc_of(f.symbol)
                if p is not None:
                    await p.live_fill(f)

            async def _bar_hook(bar, live, bf):
                p = _pc_of(getattr(bar, "symbol", ""))
                if p is not None:
                    await p.on_bar(bar, live, bf)

            async def _ghost(ts, side, qty, tag, px, symbol=""):
                p = _pc_of(symbol)
                if p is not None:
                    await p.ghost_one(ts, side, qty, tag, px)
            eng.on_live_fill = _live_fill        # mark actual fills, not decisions
            eng.on_bar_hook = _bar_hook
            eng.on_warmup_signal = _ghost        # paint ghosts as backfill replays

    # paper fills: paint (muted cyan, if painting) AND persist to claude_paper_fills
    # (if recording), tagged with the owning sleeve label.
    async def _paper_sink(f):
        p = pcs.get(f.symbol) or pc
        if p is not None and a.paper_paint:      # painting paper fills is opt-in (chart load)
            await p.paper_fill(f)
        if paper_blot is not None:
            await paper_blot.record(f, label_by_id.get(id(eng._owner.get(f.order_id)), "?"))
    eng.on_paper_fill = _paper_sink

    # ET session rollover: refresh the day-keyed snapshots so a multi-day run
    # stays correct without a restart. (1) reload the shared GammaRegime in
    # place -> every *_gex sleeve's filter sees the new prior session; (2)
    # re-fetch each painting lane's prior-session walls at the current basis.
    # RESILIENT: runs in a worker thread (never blocks the engine loop) and
    # RETRIES with backoff — a slow/laggy QuestDB read then can't silently leave
    # the regime stale for the day (the 2026-07-23 timeout).
    import time as _time

    def _retry(fn, label, tries=6):
        for i in range(tries):
            try:
                return fn(), True
            except Exception as ex:              # noqa: BLE001
                if i == tries - 1:
                    print(f"{label} GAVE UP after {tries} tries: {ex}")
                    return None, False
                _time.sleep(min(30.0, 2.0 ** i))     # 1,2,4,8,16,30s
        return None, False

    def _refresh_gamma_blocking(new_day):
        gr = _GAMMA_CACHE.get("ES")
        if gr is not None:
            _, ok = _retry(gr.reload, f"[{new_day}] gamma regime reload")
            if ok:
                print(f"[{new_day}] gamma regime reloaded (*_gex filters refreshed)")
        if a.gex_levels:
            for sym, lane_pc in pcs.items():
                lv3, ok = _retry(lambda: lane_gamma_levels(sym, new_day, a.gex_basis),
                                 f"[{new_day}] {sym} walls refresh")
                if ok and lv3 and lv3[0]:
                    lv, und, _basis = lv3
                    lane_pc.set_gamma_levels(lv)
                    # the curve is a PRIOR-session snapshot, so it has to be
                    # re-attached every rollover; otherwise a multi-day run
                    # gates forever on the book from the day it started.
                    attach_curves([s for lb, s in roster
                                   if lb.startswith(f"{sym}:") or ":" not in lb],
                                  sym, new_day)
                    print(f"[{new_day}] {sym}<-{und} walls -> putW "
                          f"{lv['put_wall']:.0f} callW {lv['call_wall']:.0f} "
                          f"({'long' if lv['net_sign']>0 else 'SHORT'}-gamma)")

    async def _on_rollover(new_day):
        # off the event loop; retries in the background until QuestDB answers
        asyncio.create_task(asyncio.to_thread(_refresh_gamma_blocking, new_day))
    eng.on_session_rollover = _on_rollover

    async def gamma_keeper():
        """Keep the gamma snapshot able to answer for TODAY, for as long as the
        engine runs.

        This engine is not meant to be restarted, so nothing about its data may
        depend on a restart. The rollover hook alone does, in three ways that are
        all silent: it is delivered through the bounded sink queue and is DROPPED
        on overflow; it fires at ET midnight while the fetch runs at 09:00 ET, so
        a late fetch misses the only reload of the day; and a reload that
        "succeeds" proves the query ran, not that any data arrived.

        So the engine asks its own snapshot a question it can answer for free --
        "can I still answer for today?" -- and acts when the answer is no. Not a
        watchdog over a bug: external data lands on someone else's schedule, and
        consuming it is the engine's job."""
        from engine.core.timeutil import et_session_date
        import time as _t
        while True:
            await asyncio.sleep(GAMMA_CHECK_S)
            gr = _GAMMA_CACHE.get("ES")
            if gr is None:
                continue
            day = et_session_date(_t.time_ns())
            if not gr.needs_reload(day):
                continue
            log.warning("GAMMA STALE for %s (newest session held: %s) -- "
                        "reloading; *_gex sleeves are failing OPEN meanwhile",
                        day, gr.newest_session())
            try:
                changed = await asyncio.to_thread(gr.reload)
            except Exception as ex:                  # noqa: BLE001
                log.error("gamma reload failed: %s (will retry in %ds)",
                          ex, GAMMA_CHECK_S)
                continue
            if gr.needs_reload(day):
                log.error("GAMMA STILL STALE for %s after a reload that %s -- "
                          "the daily fetch has not produced a usable session. "
                          "*_gex sleeves trade as their ungated twins until it "
                          "does.", day, "gained data" if changed else "changed nothing")
            else:
                log.warning("gamma refreshed for %s: gexp_prev=%.4f (newest "
                            "session %s) -- *_gex filters are live again",
                            day, gr.gexp_prev(day) or float("nan"),
                            gr.newest_session())

    # DayScore morning read (co-pilot: fade-friendliness lean + VWAP posture).
    # A moderate-tilt SIZING input, not a switch; Crabel prior-range is the most
    # robust live component (volume/overnight are noisy on the delayed feed).
    try:
        from engine.adapters.questdb import QuestDB as _SyncQDB
        from engine.features.day_score import morning_read
        # short timeout: a busy QuestDB (e.g. a replay running) must never
        # stall session startup — the day read is a nice-to-have.
        _b = _SyncQDB(timeout=10).df("SELECT ts,o,h,l,c,vol FROM claude_bars_live "
                                     "WHERE ts > dateadd('d',-40,now()) ORDER BY ts")
        _r = morning_read(_b) if len(_b) else None
        if _r:
            _c = "  ".join(f"{k} {v:.0%}" for k, v in _r["components"].items())
            _tag = "pre-market lean" if _r.get("premarket") else "last session"
            print(f"DAY READ [{_tag}] {_r['day']}: fade-friendliness {_r['score']:.0f}/100 "
                  f"[{_r['label']}]  |  posture: {_r['posture']}  |  {_c}")
            print("  (moderate-tilt sizing input, not a switch; Crabel is the robust "
                  "live component — volume/overnight are noisy on the delayed feed)")
    except Exception as ex:                          # noqa: BLE001 - never block startup
        print(f"  (day read unavailable: {ex})")

    mode = "PAPER-ONLY" if not live_owners else "LIVE(" + ",".join(live_lbls) + ")+PAPER"
    ports = "  ".join(f"{sym} m:{m['lc'].get('market_port', 36001)}"
                      f"/b:{m['lc'].get('broker_port', 36002)}"
                      for sym, m in lane_meta.items())
    print(f"live session [{mode}] {ports} "
          f"{'for %.0fs' % a.seconds if a.seconds else 'until Ctrl-C'}  "
          f"(warmup gate {'OFF' if a.no_warmup_gate else 'ON'})")

    async def stopper():
        await asyncio.sleep(a.seconds)
        print("(time cap reached, stopping)")
        eng.stop()

    async def heartbeat():
        """Print what the ENGINE is doing, not what the feed is doing.

        The old line showed obs.trades / obs.bars / blot.n_orders / blot.n_fills.
        None of those can detect the engine having stopped trading:
          * obs is the FIRST entry in `strategies`, so obs.trades increments at
            the top of the dispatch loop -- it kept climbing all through
            2026-08-04 while every sleeve behind it was dead;
          * blot is the LIVE blotter, which reads 0 all day in a paper-only
            setup, so `fills=0` was printed on every line of a normal session
            and meant nothing.
        Paper fills, dispatched events, sink drops and strategy failures are the
        numbers that go to zero when the book stops. Those are what print now.
        """
        from engine.core import dispatch as _dsp
        last_seen = -1
        last_fills = 0
        while True:
            await asyncio.sleep(5)
            state = "LIVE" if eng._live else f"WARMUP({eng._backfill_bars} bars)"
            ghosts = sum(p._n_ghost for p in pcs.values())
            pf = len(eng.paper_fills)
            fails = _dsp.strategy_failures()
            dead = [k for k, v in fails.items() if v["disabled"]]
            open_pos = sum(1 for s in strategies
                           if abs(getattr(s, "pos", 0) or 0) > 0)
            if obs.trades != last_seen or not eng._live:
                line = (f"  [{state}] ev={eng._processed} trades={obs.trades} "
                        f"bars={obs.bars} last={obs.last_px} "
                        f"paper_fills={pf}(+{pf - last_fills}) open={open_pos} "
                        f"ghosts={ghosts} live={blot.n_fills}")
                if eng._sink_dropped or eng._sink_errors:
                    line += (f" SINK drop={eng._sink_dropped} "
                             f"err={eng._sink_errors}")
                if fails:
                    line += f" STRAT_FAIL={len(fails)}"
                if dead:
                    line += f" DISABLED={','.join(k.split('@')[0] for k in dead)}"
                # A recorder whose table accepts writes and stores nothing, or
                # whose ILP writer thread has died, looks perfectly healthy from
                # every counter above. Both happened on 2026-08-05 and cost a
                # session of tape. Say it on every line until it is fixed.
                probed = list(recorders) + list(rawcaps)
                if paper_blot is not None:
                    probed.append(paper_blot)
                broken = [getattr(t, "symbol", "paper") for t in probed
                          if getattr(t, "ingest_ok", None) is False]
                if broken:
                    line += f" !!TABLE-NOT-INGESTING[{','.join(sorted(set(broken)))}]"
                stalled = [rc.symbol for rc in rawcaps if not rc.writer_alive]
                if stalled:
                    line += f" !!RAWCAP-WRITER-DEAD[{','.join(sorted(set(stalled)))}]"
                print(line)
                last_seen, last_fills = obs.trades, pf

    tasks = [asyncio.create_task(eng.run()), asyncio.create_task(heartbeat()),
             asyncio.create_task(gamma_keeper())]
    if a.seconds:
        tasks.append(asyncio.create_task(stopper()))
    try:
        await tasks[0]
    except KeyboardInterrupt:
        eng.stop()
        await asyncio.sleep(1)
    finally:
        for t in tasks[1:]:
            t.cancel()
        for ptr in painters:
            await ptr.close()

    # "recorded" means STORED. A count of rows sent into a table that swallowed
    # them is not a recording, and printing it as one is how 2026-08-04's whole
    # session looked complete the next morning.
    for rec in recorders:
        print(f"recorded [{rec.symbol}] {rec.n_recorded} 1m bars -> claude_bars_live, "
              f"{rec.n_sec} per-second rows -> claude_sec_live"
              + (f", {paper_blot.n} paper fills -> claude_paper_fills" if paper_blot else "")
              + ("" if rec.ingest_ok is not False else
                 "   *** NONE OF IT STORED: a target table is not ingesting ***"))
    for rc in rawcaps:
        print(f"raw capture [{rc.symbol}]: {rc.n_written} rows -> "
              f"claude_ticks_live/claude_depth_live"
              + (f"  (DROPPED {rc.n_dropped} — writer/DB fell behind!)" if rc.n_dropped else "")
              + ("" if rc.ingest_ok is not False else
                 "   *** NONE OF IT STORED: a target table is not ingesting ***"))
    print(f"\nsession summary: {obs.trades} trades, {obs.flows} bookflow-seconds, "
          f"{obs.bars} 1m bars ({eng._backfill_bars} backfill), last px {obs.last_px}")
    print(f"warmup orders suppressed: {eng._suppressed_orders}  |  live orders {blot.n_orders}, fills {blot.n_fills}")
    if blot.trades:
        print(blot.summary(flat_cost_pts=0.0))
    # per-sleeve PAPER activity (avg-cost realized pts, net position)
    if eng.paper_fills:
        book: dict[str, list] = {}
        for f in eng.paper_fills:
            lb = label_by_id.get(id(eng._owner.get(f.order_id)), "?")
            book.setdefault(lb, []).append((f.size, f.price))
        print(f"\npaper sleeves ({len(eng.paper_fills)} fills):")
        for lb in sorted(book):
            pos = 0; avg = 0.0; real = 0.0; n = len(book[lb])
            for q, px in book[lb]:
                if pos == 0 or (q > 0) == (pos > 0):
                    avg = (avg * abs(pos) + px * abs(q)) / (abs(pos) + abs(q)) if pos + q else px
                    pos += q
                else:
                    c = min(abs(q), abs(pos)); real += (px - avg) * (1 if pos > 0 else -1) * c
                    pos += (1 if q > 0 else -1) * c
            print(f"  {lb:16s} fills={n:3d}  realized {real:+7.1f}pt  net pos {pos:+d}")


if __name__ == "__main__":
    asyncio.run(main())
