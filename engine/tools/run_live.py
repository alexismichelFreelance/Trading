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
    if label == "ignition_gex":      # variant: entries only on short-gamma days
        return IgnitionStrategy(symbol, hmm_path, exit_mode="trailing",
                                regime_states=None, gamma=_gamma_or_none(symbol))
    if label == "opendrive":
        return OpenDriveStrategy(symbol)
    if label == "opendrive_gex":     # variant: entries only on short-gamma days
        return OpenDriveStrategy(symbol, gamma=_gamma_or_none(symbol))
    if label == "opendrive_orb":     # cleverer: opening-range break, refuses the
        return OpenDriveStrategy(symbol, mode="orb", gamma=_gamma_or_none(symbol))
        # counter-gamma break (short gamma -> won't buy the up-fakeout)
    # RIDE-then-PROTECT twins: ride untouched, then leave on momentum death /
    # range formation / retrace. Two families, because the arming rule is the
    # open question and the record should settle it, not me:
    #   _2p*      arm on 12x a trailing volatility ruler (the original)
    #   _2pN      arm at a FIXED N points
    # Measured on ~165 replay round-trips each of ESH5 and ESM5, arming distance
    # swept 0.5-1200pt: the fixed distance beat the ruler at matched arm rate on
    # every ESH5 cell and tied on ESM5, and both contracts peaked at 24-32 ES
    # points improving total AND tail. That band was read off those same curves
    # (in-sample, 30 sessions) so 16/24/32 all run and none is privileged.
    if label == "opendrive_2p":
        return OpenDriveStrategy(symbol, two_phase=TwoPhaseExit(12.0, "decay", 0.1))
    if label == "opendrive_2p_range":
        return OpenDriveStrategy(symbol, two_phase=TwoPhaseExit(12.0, "range", 0.25))
    if label == "opendrive_2p_retrace":   # strongest OOS variant
        return OpenDriveStrategy(symbol, two_phase=TwoPhaseExit(12.0, "retrace", 0.25))
    if label == "opendrive_2p16":
        return OpenDriveStrategy(symbol, two_phase=TwoPhaseExit(
            rev_kind="range", rev_f=0.25, arm_pts=16.0))
    if label == "opendrive_2p24":
        return OpenDriveStrategy(symbol, two_phase=TwoPhaseExit(
            rev_kind="range", rev_f=0.25, arm_pts=24.0))
    if label == "opendrive_2p32":
        return OpenDriveStrategy(symbol, two_phase=TwoPhaseExit(
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
    if label == "flow_gex":          # variant: increases only on short-gamma days
        return FlowFollowingStrategy(symbol, maxp=5, adaptive=True, adapt_k=FLOW_K,
                                     gate_utc=FLOW_GATE, gamma=_gamma_or_none(symbol))
    pu = INSTRUMENTS[symbol].point_usd if symbol in INSTRUMENTS else 50.0
    if label == "zones":
        return ZoneLifecycleStrategy(symbol, point_usd=pu)
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
    if label == "vwapbreak":         # study-motivated: trade the session-VWAP band break
        return VwapBreakStrategy(symbol)
    if label == "vwapbreak_gex":     # variant: breaks only on short-gamma days
        return VwapBreakStrategy(symbol, gamma=_gamma_or_none(symbol))
    if label == "vwapbreak_2p":      # ride-then-protect twin (gave back 142.25pt)
        return VwapBreakStrategy(symbol, two_phase=TwoPhaseExit(12.0, "decay", 0.1))
    if label == "vwapbreak_2p_retrace":
        return VwapBreakStrategy(symbol, two_phase=TwoPhaseExit(12.0, "retrace", 0.25))
    if label == "vwapbreak_2p24":    # fixed-distance arming (see opendrive_2pN)
        return VwapBreakStrategy(symbol, two_phase=TwoPhaseExit(
            rev_kind="range", rev_f=0.25, arm_pts=24.0))
    if label == "vwapbreak_2p32":
        return VwapBreakStrategy(symbol, two_phase=TwoPhaseExit(
            rev_kind="range", rev_f=0.25, arm_pts=32.0))
    if label == "onbreak":           # study-motivated: overnight-range break
        return OvernightBreakStrategy(symbol)
    if label == "onbreak_gex":       # variant: breaks only on short-gamma days
        return OvernightBreakStrategy(symbol, gamma=_gamma_or_none(symbol))
    if label == "onbreak_2p":        # ride-then-protect twin (gave back 74.75pt)
        return OvernightBreakStrategy(symbol,
                                      two_phase=TwoPhaseExit(12.0, "decay", 0.1))
    if label == "onbreak_2p_retrace":
        return OvernightBreakStrategy(symbol,
                                      two_phase=TwoPhaseExit(12.0, "retrace", 0.25))
    if label == "onbreak_2p24":      # fixed-distance arming (see opendrive_2pN)
        return OvernightBreakStrategy(symbol, two_phase=TwoPhaseExit(
            rev_kind="range", rev_f=0.25, arm_pts=24.0))
    if label == "onbreak_2p32":
        return OvernightBreakStrategy(symbol, two_phase=TwoPhaseExit(
            rev_kind="range", rev_f=0.25, arm_pts=32.0))
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
        if label == "trendjoin_narrow":          # half the confirmation
            return TrendJoinStrategy(symbol, conf_pts=conf / 2, stop_pts=stop)
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
ALL_LABELS = ("ignition", "ignition_fixed", "ignition_gex", "opendrive",
              "opendrive_gex", "opendrive_orb", "flow", "flow_fixed", "flow_gex",
              "zones", "zones_gap", "ibs", 
              "pivot", "vwapbreak", "vwapbreak_gex", "onbreak", "onbreak_gex",
              "rsi2", "trendjoin", "trendjoin_narrow",
              "trendjoin_2p24", "trendjoin_2p32",
              "opendrive_2p", "opendrive_2p_range", "opendrive_2p_retrace",
              "onbreak_2p", "onbreak_2p_retrace",
              "vwapbreak_2p", "vwapbreak_2p_retrace",
              # fixed-distance arming twins — the variant that measured better
              # than the volatility ruler; 16/24/32pt all run, none privileged
              "opendrive_2p16", "opendrive_2p24", "opendrive_2p32",
              "onbreak_2p24", "onbreak_2p32",
              "vwapbreak_2p24", "vwapbreak_2p32")


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
    basis = gex_basis_override if (sym == "ES" and gex_basis_override is not None) \
        else float(gexc.get("basis", 0.0))
    lv = GammaLevels(_Q(timeout=30), underlying=und).levels_prev(day, basis=basis)
    return lv, und, basis


def load_open_paper_positions(qdb, symbols) -> dict:
    """{sleeve_label: (pos, avg_px)} for paper sleeves currently holding an open
    position, avg-cost-reconstructed from the full claude_paper_fills history
    (closed round-trips net to zero and drop out). Empty on any read failure."""
    out: dict = {}
    for sym in symbols:
        try:
            pf = qdb.df(f"SELECT sleeve, side, qty, price FROM claude_paper_fills "
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
                out[str(sl)] = (pos, avg)
    return out


def build_roster(paper: str, flow_th: int = 30, symbol: str = SYMBOL,
                 hmm_path: str = HMM_PATH, prefix: str = ""):
    """Roster for one instrument lane. `prefix` namespaces labels in multi-
    instrument mode ('NQ:zones'); empty in the single-instrument default."""
    labels = list(ALL_LABELS) if paper.strip() in ("all", "") else \
        [x for x in paper.split(",") if x]
    roster = []
    for lb in labels:
        s = _make(lb, flow_th, symbol, hmm_path)
        s.label = prefix + lb        # for display / paper reporting
        roster.append((prefix + lb, s))
    return roster


async def main() -> None:
    ap = argparse.ArgumentParser()
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
            hist = daily_bars_for(sym)
            for s in pivs:
                s.seed_history(hist)
            if hist:
                print(f"seeded {len(hist)} daily bars -> {sym} pivot D/W/M grid")

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
    for sl, (pos, avg) in open_pos.items():
        s = by_label.get(sl)
        if s is not None and id(s) not in live_owners:
            eng.restore_paper_position(s, pos, avg)
            print(f"resuming paper position: {sl} {pos:+d} @ {avg:.2f} "
                  f"(at warmup->live flip)")

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
                    print(f"[{new_day}] {sym}<-{und} walls -> putW "
                          f"{lv['put_wall']:.0f} callW {lv['call_wall']:.0f} "
                          f"({'long' if lv['net_sign']>0 else 'SHORT'}-gamma)")

    async def _on_rollover(new_day):
        # off the event loop; retries in the background until QuestDB answers
        asyncio.create_task(asyncio.to_thread(_refresh_gamma_blocking, new_day))
    eng.on_session_rollover = _on_rollover

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
                broken = [t.symbol for t in (*recorders, *rawcaps)
                          if getattr(t, "ingest_ok", None) is False]
                if broken:
                    line += f" !!TABLE-NOT-INGESTING[{','.join(sorted(set(broken)))}]"
                stalled = [rc.symbol for rc in rawcaps if not rc.writer_alive]
                if stalled:
                    line += f" !!RAWCAP-WRITER-DEAD[{','.join(sorted(set(stalled)))}]"
                print(line)
                last_seen, last_fills = obs.trades, pf

    tasks = [asyncio.create_task(eng.run()), asyncio.create_task(heartbeat())]
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
