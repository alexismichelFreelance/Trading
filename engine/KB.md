# Trading Engine — Knowledge Base

Event-driven, broker/feed-agnostic ES trading engine. The same `Strategy` objects run in
backtest and live — only the Feed/Broker adapter changes. Hexagonal architecture: a pure
**core** speaks one normalized event language and knows nothing about feeds or brokers.

---

## Directory Layout

```
engine/
  core/        events, orders, ports (Protocols), clock, engine loop, config, costs, blotter
  features/    incremental causal feature engines (Gate A onward)
  strategies/  ignition, flow, zones + shared sizing (same objects run live)
  adapters/    questdb client; feeds/ (ReplayFeed, NT, recorder, merge); brokers/ (SimBroker, NT, router)
config/        instruments.yaml, frozen HMM params
tools/         fit_hmm.py, run_replay.py
tests/         unit + tests/parity (the Phase-1 gate)
bridges/       ninjatrader/ (C# relay for live path)
```

---

## Core Architecture

### Normalized Events (the only language the core speaks)

All timestamps are **epoch nanoseconds, UTC**. Side/aggressor convention: **+1 = BUY, -1 = SELL**
(signed delta = buy − sell).

| Class | Description |
|---|---|
| `Trade` | One aggressive execution. ts, price, size, aggressor (+1/-1), symbol |
| `Quote` | Top-of-book bid/ask. ts, bid, ask, bid_size, ask_size, symbol |
| `DepthUpdate` | One resting-book level change. ts, side (+1 bid/-1 ask), price, size, level, symbol |
| `Bar` | Closed OHLCV bar. ts, tf ('1m','30m','1h'), o/h/l/c/v, complete (False = partial bucket) |
| `BookFlow` | Gross resting-book flow over an interval. ts, bid_cancel, ask_cancel, bid_add, ask_add |
| `Fill` | Inbound: order fill. ts, order_id, symbol, price, size (signed), commission, slippage, tag |
| `PositionUpdate` | Inbound: position change. ts, symbol, qty (signed net), avg_px |
| `AccountUpdate` | Inbound: account state. ts, equity, realized, unrealized |
| `Signal` | Peer-channel: another strategy's INTENT (advisory only). ts, symbol, source, side, qty, tag, price, reduce_only |

**Feed primitives:** `Trade, Quote, DepthUpdate, Bar`  
**Aggregated:** `BookFlow` — computed per-second from L3/DOM in live; read from precomputed 1s table in replay  
**Inbound:** `Fill, PositionUpdate, AccountUpdate`

### Ports (Protocols — structural typing)

Defined in `engine/core/ports.py`. Adapters satisfy by shape, no inheritance required.

| Protocol | Key Methods |
|---|---|
| `FeedAdapter` | `stream() -> AsyncIterator[MarketEvent]` — strict ascending ts order |
| `BrokerAdapter` | `submit(order)`, `cancel(id)`, `modify(id, **changes)`, `events() -> AsyncIterator[BrokerEvent]` |
| `Strategy` | `on_trade/quote/depth/bar/bookflow/fill/position/signal(e) -> list[Order]` |
| `Clock` | `now() -> int`, `set(ts: int)` |

### Engine Loop

```
pull event → clock.set(ts) → broker matches resting orders → feed fills back →
dispatch to each Strategy → submit orders → feed fills back → log
```

The **Strategy instance is identical in replay and live** — the central invariant.

---

## Strategies

All strategies inherit from `BaseStrategy` and satisfy the `Strategy` protocol. Same objects
run in backtest and live.

### Primary Sleeves (Parity-Gated)

#### Ignition (`strategies/ignition.py`)

**Logic:** Detect order-flow ignition aligned with short-term trend; enter at pxc; exit via 1h HMM regime.

- **Entry:** str≥5 & avol≥800 & book-confirm(dir) & trend-aligned, inside 13-21 UTC window
- **Trend regime:** ride to nearest opposing VIRGIN 30m zone (fallback pivot), opposite-side pivot stop, −12pt cap, 600s horizon
- **Chop regime:** BOOK-healing exit (leading-side resting book rebuilt after profit peak), −4pt hard stop, 600s horizon
- **Trailing mode:** single uniform trailing stop (init −6pt, trail −10pt behind peak) — beats regime gate
- **Gamma gate:** optional `GammaRegime` — entries only on short-gamma days (trend earns there)
- **Oracle:** `strategies/ignition_oracle.py` → parity gate
- **Performance:** oracle +990.6 vs +1004 (1.3%); deployable ~+584 pts

#### Zones (`strategies/zones_strategy.py`)

**Logic:** Detect 30m S/D zones; trade three setups with half-off-at-+4 breakeven-runner scale-out.

- **FADE:** 1st touch of a fresh zone — mean-reversion (long-gamma days only via `gamma_entry_ok`)
- **BREAK:** zone busted — trend-following (short-gamma only)
- **FLIP:** retest from broken side — mean-reversion (long-gamma)
- **Exit:** scalp half at SCALP=4pt, runner leg to target or breakeven stop
- **Sizing:** RISK=$2000, K_BARS=16 bars max hold, RTH-only entries (09:30–16:00 ET)
- **Oracle:** `strategies/zones_oracle.py` → parity gate
- **Performance:** oracle +$47,502 exact; deployable ~+$4.4k ESM5

#### Flow (`strategies/flow.py`)

**Logic:** Hold position proportional to thresholded windowed aggressor-flow signal; asymmetric add/hold band; flat reset each session.

- **Signal:** rolling `w`-second window of signed delta (buy−sell volume), threshold `th`
- **Position:** `tgt = F / scale`; bounded by `maxp` contracts
- **Banding:** add band (enter) vs hold/reduce band — reduces keep full band
- **Adaptive mode:** threshold scales with rolling vol (NOT validated as more robust)
- **Gamma gate:** position INCREASES only on short-gamma days; reduces always pass
- **Oracle:** `strategies/flow_oracle.py` → parity gate
- **Performance:** oracle +811.3 vs +814 (0.3%)

### Secondary Sleeves

#### Open Drive (`strategies/open_drive.py`)

- At 10:00 ET: enter in direction of 9:30→10:00 move; stop = 1.0× morning range (floor 5), trail = 1.5× (floor 8), flat at 16:00
- Price-only (no book/flow data), one decision/day
- Evidence: +815 pts (4 months+, both contracts positive, daily ρ=−0.01 vs ignition)
- Parity: `tests/parity/test_parity_opendrive.py`

#### IBS Swing (`strategies/ibs_swing.py`)

- At 15:59 ET: buy MOC when IBS<0.2; sell MOC when IBS>0.8; no intraday stop
- Tail risk via SIZING: 1-lot maxDD −$24.5k over 16y
- **Evidence:** ES=F 2010→2026, n=435, +4644pt net, t=+4.2, win 70%, H2>H1 (no decay)
- **GEX sizing:** 2 lots long-gamma (win 78%) vs 1 short-gamma (worst −347) → +72% total
- Parity: `tests/parity/test_parity_ibs.py` (needs `tools/fetch_daily.py` first)

#### Other strategies (experimental/research):
- `dip_buy.py`, `fade_ladder.py`, `fade_turn.py`, `macro_dip.py`, `overnight_break.py`,
  `overnight_fade.py`, `pivot.py`, `rsi2_swing.py`, `sweep_follow.py`, `trend_join.py`,
  `trend_join_backup.py`, `vwap_break.py`, `wall_fade.py`

---

## Feature Engines

All features are **incremental and causal** — no look-ahead, no indexing into the future.
Located in `engine/features/`.

| Module | Purpose |
|---|---|
| `bars.py` | `BarAggregator` — aggregates OHLCV into multiple timeframes (e.g., 30m, 1h) |
| `online.py` | `IgnitionFeatures` — incremental STR, AVOL, pxc, book-confirm per second |
| `hmm.py` | `GaussianHMM2` — frozen 2-state Gaussian HMM, online forward-filter per-session; `fit_gaussian_hmm` for offline fitting |
| `zones.py` | `ZoneDetector` (finds 30m S/D zones), `ZoneBook` (manages active zones) |
| `pivots.py` | `SessionLevels` — pivot highs/lows, session targets/stops |
| `efficiency.py` | `OnlineKaufmanER`, `RollingEfficiency` — causal Kaufman Efficiency Ratio |
| `avwap.py` | AVWAP (Anchor Volume-Weighted Average Price) |
| `bookflow.py` | Per-second BookFlow aggregation from live DOM |
| `gamma.py` | `GammaRegime` (SqueezeMetrics aggregate GEX), `NDXGammaRegime` (CBOE true-OI) — causal prior-session trailing-252 percentile; trend_max_pctl=1/3 |
| `gamma_curve.py` | `GammaCurve` — sign of cumulative gamma AT PRICE (local flip vs percentile) |
| `gamma_levels.py`, `gamma_profile.py`, `gamma_basis.py` | Gamma-related utilities |
| `break_quality.py` | Breakout quality metrics |
| `day_range.py` | `DayRange` — session range tracking for scale-out decisions |
| `day_score.py` | Daily regime scoring |
| `swings.py` | Swing detection |
| `level_book.py` | Level book management |

### Gamma / GEX

**GammaRegime** reads from `claude_gex` (SqueezeMetrics aggregate GEX). Data fetched by
`tools/fetch_gex.py`. **CAUSAL:** the regime for day D is the prior session's trailing-252
percentile of gexp. Low percentile = short-gamma = trend amplified; high = long-gamma = mean-reversion.

**NDXGammaRegime** reads from `claude_gex_levels` (CBOE true-OI, ≤7 DTE), fetched by
`tools/fetch_cboe_gex.py`.

Staleness protection: `needs_reload(day)` and `MAX_AGE_DAYS=4`. Stale data degrades to None
(fails open, so a stale pipeline behaves as ungated rather than on fiction).

**GammaCurve** reads sign of cumulative gamma AT PRICE — `sign_at(px)` — rather than the
aggregate percentile. Near a pocket edge (within `pocket_min_edge` points), trend continuation
fails in BOTH regimes.

---

## Data Sources / Adapters

### QuestDB (`adapters/questdb.py`, `adapters/feeds/replay_questdb.py`)

**ReplayFeed** streams QuestDB history as normalized events in ts order.

- Per-second: `claude_sec_feat` → BUY/SELL Trades + BookFlow per second
- Optional 1m bars: `claude_bars_1m` → emitted at bar close (causal, ordered before same-second flow)
- Columns used: `ts, pxc, adelta, avol, bid_cancel, ask_cancel, bid_add, ask_add`
- **Forward-looking columns (hi/lo/nxt) are NEVER read**

### NinjaTrader (`adapters/feeds/ninjatrader.py`, `adapters/brokers/ninjatrader.py`)

Live path: `LiveEngine` drives strategies async off N `NinjaTraderFeed`s + `NinjaTraderBroker`s
over local JSON sockets (`adapters/protocol.py`). Per-second `BookFlow` aggregated from live DOM.
C# relay in `bridges/ninjatrader/`.

### Other Adapters

| Module | Purpose |
|---|---|
| `feeds/merge.py` | Merge multiple feeds |
| `feeds/recorder_tee.py` | Capture/replay recorded data |
| `feeds/raw_capture.py` | Raw feed capture |
| `brokers/sim.py` | `SimBroker` — internal fill model for replay |
| `brokers/router.py` | Order routing |
| `brokers/socket_broker.py` | Socket-based broker |
| `paper_blotter.py` | Paper trading blotter |
| `painter.py` | Chart painting / visualization |

---

## Configuration

### `config/instruments.yaml`
Instrument definitions (symbol, point value, tick size, etc.)

### `config/hmm_es_1h.json`
Frozen 2-state Gaussian HMM parameters (pi, A, means, vars, trend_state). Fit offline on
ESM5 1h Kaufman-efficiency via `tools/fit_hmm.py`.

### `config/hmm_1h_states.json`
Frozen per-hour regime states (0=chop, 1=trend). Used for parity gates. Regenerated by
`tools/gen_states.py`.

### `config/live.yaml`
Live trading configuration (roster, adapters, thresholds)

### `config/macro_gate.json`, `config/macrodip_seed_*.json`, `config/rsi2_seed_*.json`
Regime gate and strategy seed data for macro dip, RSI2 strategies.

### `config/promotion.yaml`
Promotion/advancement criteria.

---

## Run Commands

```bash
# Unit + parity tests
.venv/Scripts/python.exe -m pytest -q

# Fit & freeze 1h HMM (writes config/hmm_es_1h.json)
.venv/Scripts/python.exe tools/fit_hmm.py

# Run replay with a strategy
.venv/Scripts/python.exe tools/run_replay.py --strategy ignition --all-months
```

**Requires:** QuestDB at http://localhost:9000

---

## Parity Gates (Phase 1)

All gates PASS:

| Gate | Description | Result |
|---|---|---|
| A (exact) | Online `str/dir/pxc/avol` == precomputed columns (bit-for-bit) | ✅ PASS |
| B (ignition) | Oracle +990.6 vs engine +1004 (1.3%), all 4 months | ✅ PASS |
| C (zones) | Oracle +$47,502 vs engine +$47,502 (exact) | ✅ PASS |
| D (flow) | Oracle +811.3 vs engine +814 (0.3%), all 4 months | ✅ PASS |

Run: `pytest tests/parity --run-parity -s`

---

## Fill Model (`SimBroker`)

| Order Type | Fill Rule |
|---|---|
| MARKET | At current ref_price + adverse slippage (or `trigger_price` if set) |
| LIMIT | Rests; fills at limit price when price crosses through (no slippage) |
| STOP | Rests; triggers when price crosses; then fills at stop price + adverse slippage |

Pluggable fill models:
- `CleanFill` — no slippage, no commission (for ignition/flow parity where flat 0.517pt cost applied at reporting)
- `RegimeCostFill` — NinjaTrader commission + regime-coherent slippage (for sized zone sleeve)

---

## Key Conventions

- **All timestamps** are epoch nanoseconds, UTC
- **Side convention:** +1 = BUY (buyer aggressor), −1 = SELL (seller aggressor)
- **DepthUpdate.side:** +1 = bid side, −1 = ask side
- **ET gates** must use `et_minute_of_day()` — fixed UTC hours silently shift at DST
- **Gamma data** degrades to None if stale >4 days; fails open (no gating)
- **Strategy instance is identical** in replay and live (only adapter changes)
- **Oracle vs Strategy:** oracle reproduces research numbers (per-signal); strategy is the
  deployable single-position form with realistic fills
- **Signal** is advisory peer-channel only (intent, never position state)
- **Scale-out:** take half at scale threshold; keeps upside on range-expanding days

---

## Glossary

| Term | Definition |
|---|---|
| pxc | Price at center of second (volume-weighted average) |
| avol | Cumulative volume per second |
| adelta | Signed delta (buy volume − sell volume) per second |
| STR | Short-term strength ratio (flow directional measure) |
| IBS | Intraday Breadth Signal: (close − low) / (high − low) |
| GEX | Gamma Exposure (SqueezeMetrics aggregate) |
| MOC | Market-On-Close order |
| RTH | Regular Trading Hours |
| zone | 30m support/demand zone (detected by departure from prior range) |
| BookFlow | Gross resting-book add/cancel flow over an interval, split by side |
| HMM | Hidden Markov Model (2-state Gaussian for trend/chop regime) |
| ER | Kaufman Efficiency Ratio |
