"""Portfolio replay — the whole roster against one tape, with the peer channel on.

Every study so far ran ONE sleeve in isolation. That cannot see the two things
that actually decide portfolio behaviour:

  * CONCENTRATION. On 2026-07-27 the engine made +$16,852 and ~all of it was a
    single directional idea expressed nine ways -- ES onbreak, onbreak_gex,
    opendrive and opendrive_gex returned IDENTICALLY +26.25pt. That is not nine
    sleeves agreeing, it is one trade counted nine times, and no single-sleeve
    backtest can show it.
  * PEER EXITS. tools/exit_thesis_lab.py could measure the thesis (T) and
    invalidation (I) exits but NOT the peer exit (O), because O needs other
    sleeves running against the same tape. This provides that.

It reuses LiveEngine itself rather than ReplayEngine. ReplayEngine broadcasts
fills to every strategy (dispatch_broker(self.strategies, be)) -- correct for
single-sleeve parity, wrong for a roster, where each sleeve must own its book.
LiveEngine already has per-strategy ownership, the paper-fill path and the
Signal channel, so driving it from a historical feed means the replay IS the
live code path, not a reimplementation of it.

Trades come from mbo_events (REAL ticks). That matters: sweepfade clusters the
tape at 1ms, so the per-second aggregates in claude_sec_feat cannot drive it.
Bars come from claude_bars_1m for the bar-driven sleeves.

    .venv/Scripts/python.exe tools/portfolio_replay.py --symbol ESM5 --days 5
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB                      # noqa: E402
from engine.core.blotter import Blotter                          # noqa: E402
from engine.core.clock import EventClock                         # noqa: E402
from engine.core.events import BUY, SELL, Bar, BookFlow, Trade             # noqa: E402
from engine.core.live_engine import LiveEngine                   # noqa: E402
from tools.flow_replication import session_days                  # noqa: E402
from tools.run_live import ALL_LABELS, _make, attach_day_range                     # noqa: E402

from zoneinfo import ZoneInfo as _ZI
_ET = _ZI("America/New_York")

_MIN_NS = 60 * 1_000_000_000
POINT_USD = 50.0        # ES
POINT_USD_OF = {"ES": 50.0, "NQ": 20.0, "ESM5": 50.0, "ESH5": 50.0}

# Reloading ~540k mbo_events rows per run took 257s and is what put QuestDB down
# on 2026-07-28. Each session is fetched ONCE and parked on disk after that.
CACHE_DIR = ROOT / ".cache" / "replay"



def et_session_bounds_utc(day: str) -> tuple[str, str]:
    """UTC bounds of the ET calendar day `day` (YYYY-MM-DD), as QuestDB literals.

    The fetches used to filter `ts >= '{day}T00:00:00Z' AND ts < '{day}T23:59:59Z'`
    -- a UTC window -- while _live_days() hands out ET session dates. For ET date
    D that window is really ET 20:00 of D-1 through 19:59 of D, so every replayed
    "day" opened with the PREVIOUS evening's bars. run_day builds a fresh
    strategy per day, so a swing sleeve met that 20:00 ET bar first, found minute
    1200 past its 15:59 DECISION_MIN with `_decided` False, and committed the
    whole day's decision to one evening bar (two ibs entries on 2026-07-29, one
    exit).

    Anchoring in ET also survives DST: a fixed UTC offset would shift by an hour
    either side of a change and quietly mis-slice two sessions a year.
    """
    d = _dt.date.fromisoformat(day)
    lo = _dt.datetime.combine(d, _dt.time(0, 0), tzinfo=_ET)
    hi = lo + _dt.timedelta(days=1)
    f = "%Y-%m-%dT%H:%M:%S.%f"
    return (lo.astimezone(_dt.timezone.utc).strftime(f)[:-3] + "Z",
            hi.astimezone(_dt.timezone.utc).strftime(f)[:-3] + "Z")


class HistFeed:
    finite = True      # bounded: stream-end means DONE, not a disconnect
    """Historical events for one session, in ts order, as a live-shaped feed."""

    def __init__(self, events):
        self._events = events

    async def stream(self):
        for e in self._events:
            yield e


class NullBroker:
    """All sleeves are PAPER here, so no order ever reaches a broker. It only
    has to exist and end its event stream cleanly."""

    finite = True      # bounded: stream-end is DONE, not a disconnect

    async def events(self):
        return
        yield           # pragma: no cover - makes this an async generator

    async def submit(self, o):        # pragma: no cover - never called on paper
        raise AssertionError("portfolio replay is paper-only; nothing routes live")


def load_events(qdb: QuestDB, symbol: str, day: str, source: str = "mbo") -> list:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cf = CACHE_DIR / f"{symbol}_{day}{'_live' if source == 'live' else ''}.npz"
    if cf.exists():
        z = np.load(cf)
        return _events_from(pd.DataFrame({k: z[k] for k in z.files}), symbol)
    df = _fetch_live(qdb, symbol, day) if source == "live" else _fetch(qdb, symbol, day)
    if df is not None and len(df):
        np.savez_compressed(cf, **{c: df[c].to_numpy() for c in df.columns})
        return _events_from(df, symbol)
    return []


def _events_from(df: pd.DataFrame, symbol: str) -> list:
    """Build the event objects from NUMPY arrays, not per-row .iloc.

    This is where the time actually went: 540k rows x 6 fields of `.iloc[i]`
    cost ~290s, which is why adding a disk cache barely helped (327s -> 290s).
    The DB was never the bottleneck; pandas scalar indexing was. Pull each
    column out once and index the arrays.
    """
    ts = df["ts"].to_numpy(dtype="int64")
    kind = df["kind"].to_numpy(dtype="int8")
    a = df["a"].to_numpy(dtype=float)
    b = df["b"].to_numpy(dtype=float)
    c = df["c"].to_numpy(dtype=float)
    d = df["d"].to_numpy(dtype=float)
    e = df["e"].to_numpy(dtype="int64")
    order = np.lexsort((kind, ts))            # ts asc, bars (kind 0) before trades
    out = []
    for i in order:
        t = int(ts[i])
        k = int(kind[i])
        if k == 1:
            out.append(Trade(t, float(a[i]), int(b[i]), int(c[i]), symbol))
        elif k == 0:
            out.append(Bar(t, "1m", float(a[i]), float(b[i]), float(c[i]),
                           float(d[i]), int(e[i]), symbol))
        elif k == 2:
            # per-second book pressure. Without this ignition and flow see no
            # BookFlow at all and produce NO ROWS -- an absent result that reads
            # exactly like a flat one, which is how their gate went unverified.
            out.append(BookFlow(t, float(a[i]), float(b[i]), float(c[i]),
                                float(d[i]), symbol))
        else:
            # `else means Bar` is what hid the missing kind. An unknown kind is
            # a decoder bug; mis-typing it into the tape is worse than stopping.
            raise ValueError(f"unknown replay event kind {k!r} at ts {t}")
    return out


def _fetch(qdb: QuestDB, symbol: str, day: str):
    _lo, _hi = et_session_bounds_utc(day)
    """Real ticks + 1m bars, merged. Bars are emitted at their CLOSE and ordered
    BEFORE same-instant trades, exactly as ReplayFeed does, so coarse features
    update before the fine ones."""
    """Real ticks + 1m bars for one session, flattened for the parquet cache."""
    tr = qdb.df("SELECT ts_recv, price, size, side FROM mbo_events "
                f"WHERE action='T' AND symbol='{symbol}' "
                f"AND ts_recv >= '{_lo}' "
                f"AND ts_recv <  '{_hi}' ORDER BY ts_recv")
    if tr.empty:
        return None
    rows = pd.DataFrame({
        "ts": pd.to_datetime(tr["ts_recv"]).astype("int64"),
        "kind": 1,
        "a": tr["price"].astype(float),
        "b": tr["size"].astype(float),
        "c": np.where(tr["side"].to_numpy() == "B", BUY, SELL),
        "d": 0.0, "e": 0})
    br = qdb.df("SELECT ts,o,h,l,c,vol FROM claude_bars_1m "
                f"WHERE symbol='{symbol}' "
                f"AND ts >= '{_lo}' "
                f"AND ts <  '{_hi}' ORDER BY ts")
    if len(br):
        rows = pd.concat([rows, pd.DataFrame({
            "ts": pd.to_datetime(br["ts"]).astype("int64") + _MIN_NS,
            "kind": 0, "a": br["o"].astype(float), "b": br["h"].astype(float),
            "c": br["l"].astype(float), "d": br["c"].astype(float),
            "e": br["vol"].astype(int)})], ignore_index=True)
    return rows.sort_values("ts").reset_index(drop=True)


def _fetch_live(qdb: QuestDB, symbol: str, day: str):
    _lo, _hi = et_session_bounds_utc(day)
    """Same shape as _fetch, but from the LIVE capture tables (claude_ticks_live /
    claude_bars_live, symbol 'ES'/'NQ') instead of mbo_events + claude_bars_1m.

    This exists to REBUILD claude_paper_fills. Until 2026-07-29 a live paper fill
    was stamped from the wall clock while priced from the event stream, so 51% of
    the recorded rows held a price the market never traded in the minute claimed
    (tools/paper_audit.py). The captured market data was never affected -- bars
    are complete for every session, zero missing trading minutes -- so the record
    is regenerable: replay the real tape through the same LiveEngine under an
    EventClock, where clock.set() actually works and every fill is stamped by the
    event that priced it.
    """
    tr = qdb.df("SELECT ts, price, size, aggressor FROM claude_ticks_live "
                f"WHERE symbol='{symbol}' "
                f"AND ts >= '{_lo}' "
                f"AND ts <  '{_hi}' ORDER BY ts")
    br = qdb.df("SELECT ts,o,h,l,c,vol FROM claude_bars_live "
                f"WHERE symbol='{symbol}' "
                f"AND ts >= '{_lo}' "
                f"AND ts <  '{_hi}' ORDER BY ts")
    # PER-SECOND BOOK PRESSURE. Recorded since 2026-07-14 for exactly this and
    # never wired in, so ignition and flow saw no BookFlow, took no trades and
    # produced no rows -- an ABSENT result that reads like a flat one. That is
    # how their gamma gate sat in the roster for weeks without ever being
    # evaluated against a counterfactual.
    sec = qdb.df("SELECT ts, bid_cancel, ask_cancel, bid_add, ask_add "
                 "FROM claude_sec_live "
                 f"WHERE symbol='{symbol}' "
                 f"AND ts >= '{_lo}' "
                 f"AND ts <  '{_hi}' ORDER BY ts")
    if tr.empty and br.empty:
        return None
    parts = []
    if len(sec):
        parts.append(pd.DataFrame({
            "ts": pd.to_datetime(sec["ts"]).astype("int64"),
            "kind": 2,
            "a": sec["bid_cancel"].astype(float),
            "b": sec["ask_cancel"].astype(float),
            "c": sec["bid_add"].astype(float),
            "d": sec["ask_add"].astype(float),
            "e": 0}))
    if len(tr):
        parts.append(pd.DataFrame({
            "ts": pd.to_datetime(tr["ts"]).astype("int64"),
            "kind": 1,
            "a": tr["price"].astype(float),
            "b": tr["size"].astype(float),
            "c": tr["aggressor"].astype(float),
            "d": 0.0, "e": 0}))
    if len(br):
        # NO +_MIN_NS here, unlike _fetch. The two bar tables use OPPOSITE
        # timestamp conventions, verified by joining each to its own tick source
        # at offsets -2..+2 minutes:
        #   claude_bars_1m   (research) ts = bar OPEN  -> needs +1min to emit at
        #                    close (offset 0 puts 0/71,940 ESM5 ticks outside)
        #   claude_bars_live (capture)  ts = bar CLOSE -> already correct
        #                    (offset +1 puts 1/53,546 ES ticks outside; offset 0
        #                     leaves 34% out)
        # Adding a minute here shifted every bar-driven fill one minute late.
        parts.append(pd.DataFrame({
            "ts": pd.to_datetime(br["ts"]).astype("int64"),
            "kind": 0, "a": br["o"].astype(float), "b": br["h"].astype(float),
            "c": br["l"].astype(float), "d": br["c"].astype(float),
            "e": br["vol"].astype(int)}))
    return pd.concat(parts, ignore_index=True).sort_values("ts").reset_index(drop=True)


def pnl_of(fills, point_usd: float) -> float:
    pos, cost, real = 0, 0.0, 0.0
    for f in fills:
        qd = int(f.size)
        while qd:
            if pos and (pos > 0) != (qd > 0):
                n = min(abs(qd), abs(pos))
                sgn = 1 if pos > 0 else -1
                real += n * (f.price - cost) * sgn
                pos -= n * sgn
                qd -= n * (1 if qd > 0 else -1)
            else:
                tot = abs(pos) + abs(qd)
                cost = (cost * abs(pos) + f.price * abs(qd)) / tot
                pos += qd
                qd = 0
    return real * point_usd


def build_strategies(symbol, labels, peer_map):
    """One strategy object per label, built ONCE for the whole replay.

    run_day used to construct fresh instances every session. For an intraday
    sleeve that is harmless -- they reset on their own session rollover -- but it
    makes a SWING sleeve impossible to evaluate: IBS enters Monday and exits
    Thursday, so with a new object each day the position never closes, nothing is
    ever realised, and the report shows 0 days / $0 even though fills were
    written (15 fills, both sleeves reported empty, 2026-06/08 ES live).

    Carrying the objects across days is also what the LIVE engine does, so the
    replay now matches it instead of quietly modelling a different program.

    STILL NOT ENOUGH -- swing sleeves remain unevaluable here. Position state
    lives in the ENGINE (LiveEngine._pos / Blotter), and run_day builds a fresh
    engine and blotter per session. So a strategy's own self.pos survives the day
    boundary but the engine's does not, and the reduce_only exit it emits on day
    D+3 is applied against an engine that believes it is flat. Verified after
    this change: ibs/rsi2 still report 0 days / $0.

    Making this work needs the engine and blotter to span the replay too, with
    the feed chained across sessions rather than restarted -- i.e. replay one
    CONTINUOUS stream and cut the reporting by session, instead of running N
    independent one-day engines. Until then:
        * intraday sleeves (trendjoin, opendrive, onbreak, vwapbreak...) are
          measured correctly here -- they open and close inside a session;
        * ibs and rsi2 are NOT, and every replay figure for them should be
          treated as absent, not as zero.
    The 16-year IBS evidence comes from strategy_lab/ibs_verify.py, which does
    not use this harness and is unaffected.
    """
    out = []
    for lb in labels:
        s = _make(lb, symbol=symbol)
        s.label = f"{symbol}:{lb}"
        if lb in peer_map and hasattr(s, "peer_exit"):
            s.peer_exit = tuple(f"{symbol}:{p}" for p in peer_map[lb])
        out.append(s)
    return out


_CURVE_CACHE: dict = {}


def _attach_curves(qdb, strats, symbol: str, day: str) -> None:
    """Give every pocket-gated sleeve the curve for `day`, or None.

    Cached per (symbol, day) so a 30-session replay does not re-read the strike
    table once per sleeve. Fail-open: any failure leaves pocket=None, which
    BaseStrategy.pocket_entry_ok treats as no filter."""
    want = [s for s in strats if getattr(s, "pocket_min_edge", 0.0) > 0.0
            or getattr(s, "_wants_curve", False)
            or type(s).__name__ == "WallFadeStrategy"]
    if not want:
        return
    key = (symbol, day)
    if key not in _CURVE_CACHE:
        try:
            from engine.core.config import root_symbol
            from engine.features.gamma_basis import BasisSeries
            from engine.features.gamma_curve import GammaCurve
            root = root_symbol(symbol)
            und = {"ES": "SPX", "NQ": "NDX"}.get(root)
            if und is None:
                _CURVE_CACHE[key] = None
            else:
                basis = BasisSeries.load(qdb, und, root, 0.0).for_day(day)
                c = GammaCurve.load_prev(qdb, day, basis, underlying=und)
                _CURVE_CACHE[key] = c if c.ok else None
        except Exception:                              # noqa: BLE001
            _CURVE_CACHE[key] = None
    for s in want:
        s.pocket = _CURVE_CACHE[key]


async def run_day(qdb, symbol, day, labels, peer_map, source="mbo",
                  min_events=10_000, strats=None):
    ev = load_events(qdb, symbol, day, source)
    if len(ev) < min_events:
        return None
    if strats is None:                      # standalone use keeps old behaviour
        strats = build_strategies(symbol, labels, peer_map)
        attach_day_range(strats, symbol)
    # Hand the pocket-gated sleeves the PRIOR session's curve. Strategy objects
    # persist across the replay (see build_strategies), so the curve has to be
    # refreshed per session or every day would be gated on the first day's book.
    _attach_curves(qdb, strats, symbol, day)
    # DayRange is attached ONCE, outside the per-day loop, and deliberately so:
    # it carries PRIOR sessions' ranges, so re-creating it per day would leave
    # it permanently cold -- size_mult would always fail closed to one lot and
    # the sizing twin would be indistinguishable from the plain sleeve. The
    # strategy objects persist across the replay for the same reason (see
    # build_strategies), and the indicator rolls its own session on the first
    # event of a new day.
    eng = LiveEngine(HistFeed(ev), NullBroker(), strats, EventClock(),
                     Blotter(symbol, POINT_USD_OF.get(symbol, POINT_USD),
                             ), warmup_gate=False,
                     live_owners=set())        # set() = every sleeve is PAPER
    await eng.run()
    by = {}
    for f in eng.paper_fills:
        s = eng._owner.get(f.order_id)
        by.setdefault(eng.label_of(s), []).append(f)
    flat = []
    for f in eng.paper_fills:
        st = eng._owner.get(f.order_id)
        flat.append({"ts": f.ts, "symbol": f.symbol, "sleeve": eng.label_of(st),
                     "side": 1 if f.size > 0 else -1, "qty": abs(int(f.size)),
                     "price": f.price, "tag": f.tag})
    return {"day": day, "fills": by, "flat": flat, "signals": len(eng.signals),
            "peer_exits": eng._peer_exits, "n_events": len(ev)}


def report(rows, symbol, point_usd, labels):
    print("\n" + "=" * 84)
    print(f"PORTFOLIO REPLAY — {symbol}, {len(rows)} sessions, "
          f"{len(labels)} sleeves, peer channel ON")
    print("=" * 84)
    tot_sig = sum(r["signals"] for r in rows)
    tot_px = sum(r["peer_exits"] for r in rows)
    print(f"\nsignals broadcast {tot_sig:,}   peer-triggered exits {tot_px}")

    daily: dict[str, dict[str, float]] = {}
    for r in rows:
        for lb, fl in r["fills"].items():
            daily.setdefault(lb, {})[r["day"]] = pnl_of(fl, point_usd)
    if not daily:
        print("no sleeve produced fills")
        return
    df = pd.DataFrame(daily).fillna(0.0)

    print(f"\n{'sleeve':>22} {'days':>5} {'total $':>10} {'mean $':>9} {'sign%':>7}")
    for lb in df.columns[np.argsort(-df.sum().to_numpy())]:
        v = df[lb].to_numpy()
        nz = v[v != 0]
        sign = 100 * max((nz > 0).mean(), (nz < 0).mean()) if len(nz) else 0
        print(f"{lb:>22} {len(nz):5d} {v.sum():10,.0f} {v.mean():9,.0f} {sign:6.1f}%")
    print(f"{'PORTFOLIO':>22} {len(df):5d} {df.to_numpy().sum():10,.0f}")

    # ── concentration: is this a portfolio or one trade wearing many hats? ──
    print("\n[CONCENTRATION] pairwise daily-P&L correlation, |rho| >= 0.9")
    c = df.corr().fillna(0.0)
    dup = []
    for i, a in enumerate(c.columns):
        for b in c.columns[i + 1:]:
            if abs(c.loc[a, b]) >= 0.9:
                dup.append((a, b, c.loc[a, b]))
    for a, b, r in sorted(dup, key=lambda x: -abs(x[2]))[:15]:
        print(f"    {r:+.3f}  {a} ~ {b}")
    if not dup:
        print("    none -- sleeves are genuinely independent")
    tot = df.to_numpy().sum()
    top = df.sum().sort_values(ascending=False)
    if abs(tot) > 1e-9 and len(top):
        print(f"\n    top sleeve = {100*top.iloc[0]/tot:5.1f}% of portfolio P&L")
        print(f"    top 3      = {100*top.head(3).sum()/tot:5.1f}%")
    print("=" * 84 + "\n")


def _live_days(qdb: QuestDB, symbol: str) -> list[str]:
    """ET session dates that actually have live capture for this instrument."""
    b = qdb.df("SELECT ts FROM claude_bars_live "
               f"WHERE symbol='{symbol}' ORDER BY ts")
    if b.empty:
        return []
    t = pd.to_datetime(b.ts, utc=True).dt.tz_convert("America/New_York")
    return sorted(t.dt.strftime("%Y-%m-%d").unique())


def _select(only: str) -> list[str]:
    """Which sleeves to replay.

    Running all 36 costs ~8M no-op callbacks per session for the bar-only
    sleeves, because every one of ~0.5-1.3M tick events is dispatched to every
    strategy. Validating one sleeve family should not pay for the other 32:
    a 50-day ESM5 run over the April 2025 crash days took 19 HOURS all-in.

    --labels accepts exact names or prefixes, comma separated:
        --labels trendjoin          every trendjoin* variant
        --labels ibs,rsi2           just the swing sleeves
    """
    base = [lb for lb in ALL_LABELS if lb not in ("flow_fixed",)]
    if not only:
        return base
    want = [w.strip() for w in only.split(",") if w.strip()]
    sel = [lb for lb in base if lb in want or any(lb.startswith(w) for w in want)]
    if not sel:
        raise SystemExit(f"--labels {only!r} matched nothing. available: "
                         + ", ".join(base))
    return sel


async def main_async(symbol, ndays, peer_map, source="mbo", only="", until=""):
    qdb = QuestDB(timeout=240.0)
    labels = _select(only)
    rows, allflat = [], []
    days = (_live_days(qdb, symbol) if source == "live"
            else session_days(symbol, None, None))
    # PIN THE WINDOW. --days N alone takes the last N sessions PRESENT IN THE
    # DATABASE, which moves every time a new session is recorded. Two runs a day
    # apart then cover different days, and the difference gets read as an effect
    # of whatever code changed in between: on 2026-08-13 that made nine sleeves
    # I had not touched appear to move, and sent me hunting for a coupling bug
    # that did not exist. An A/B against a rolling window is not an A/B.
    if until:
        days = [d for d in days if d <= until]
    # live capture has far fewer events than mbo_events; do not skip real days
    minev = 500 if source == "live" else 10_000
    strats = build_strategies(symbol, labels, peer_map)
    attach_day_range(strats, symbol)
    for d in days[-ndays:]:
        try:
            r = await run_day(qdb, symbol, d, labels, peer_map, source, minev,
                              strats=strats)
        except Exception as ex:                       # noqa: BLE001
            print(f"  {d}: FAILED {type(ex).__name__}: {str(ex)[:70]}", flush=True)
            continue
        if r:
            rows.append(r)
            allflat.extend(r["flat"])
            print(f"  {d}: {r['n_events']:,} events, "
                  f"{sum(len(v) for v in r['fills'].values())} fills, "
                  f"{r['peer_exits']} peer exits", flush=True)
    if not rows:
        raise SystemExit("no sessions replayed")
    if allflat:
        out = ROOT / ".cache" / f"replay_fills_{symbol}{'_live' if source=='live' else ''}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(allflat).to_csv(out, index=False)
        print(f"\n  wrote {len(allflat)} replay fills -> {out}")
    report(rows, symbol, POINT_USD_OF.get(symbol, POINT_USD), labels)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ESM5")
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--source", choices=("mbo", "live"), default="mbo",
                    help="'live' rebuilds from claude_ticks_live/claude_bars_live "
                         "(symbol ES/NQ) -- use this to regenerate the corrupted "
                         "claude_paper_fills record")
    ap.add_argument("--until", default="",
                    help="last session date to include (YYYY-MM-DD). Pins the "
                         "window so two runs are comparable; without it --days "
                         "slides forward as new sessions are recorded.")
    ap.add_argument("--labels", default="",
                    help="replay only these sleeves (exact names or prefixes, "
                         "comma separated). Default: the whole roster.")
    ap.add_argument("--peer-exit", default="",
                    help="sleeve:peer1|peer2,... e.g. sweepfade:onbreak|opendrive")
    a = ap.parse_args()
    pm = {}
    for part in filter(None, a.peer_exit.split(",")):
        k, _, v = part.partition(":")
        pm[k] = [x for x in v.split("|") if x]
    asyncio.run(main_async(a.symbol, a.days, pm, a.source, a.labels, a.until))


if __name__ == "__main__":
    main()
