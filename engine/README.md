# es-engine — event-driven, broker/feed-agnostic ES trading engine

The **same `Strategy` code runs in backtest and live**; only the Feed/Broker
adapter changes. A small pure **core** speaks one normalized event language and
knows nothing about brokers or feeds (hexagonal / ports-and-adapters).

## Layout
```
engine/core/        events, orders, ports (Protocols), clock, engine loop, config, costs, blotter
engine/features/    incremental causal feature engines (Gate A onward)
engine/strategies/  ignition, flow, zones + shared sizing  (same objects run live)
engine/adapters/    questdb client; feeds/ (ReplayFeed, NT, recorder, merge); brokers/ (SimBroker, NT, router)
config/             instruments.yaml, frozen HMM params
tools/              fit_hmm.py, run_replay.py
tests/              unit + tests/parity (the Phase-1 gate)
```

## Normalized events (the only language the core speaks)
`Trade, Quote, DepthUpdate, Bar` (feed primitives), `BookFlow` (gross resting-book
add/cancel flow over an interval — live adapters aggregate it from raw L3/DOM,
the replay feed reads it from the precomputed 1s table), and inbound `Fill,
PositionUpdate, AccountUpdate`. All timestamps are epoch **nanoseconds, UTC**.
Side/aggressor convention: **+1 buy, -1 sell** (signed delta = buy − sell).

## Engine loop
`pull event → clock.set(ts) → broker matches resting orders → feed fills back →
dispatch to each Strategy → submit orders → feed fills back → log`. The Strategy
instance is identical in replay and live — the central invariant.

## Run
```bash
.venv/Scripts/python.exe -m pytest -q          # unit + parity tests
.venv/Scripts/python.exe tools/fit_hmm.py      # fit & freeze the 1h HMM (writes config/hmm_es_1h.json)
.venv/Scripts/python.exe tools/run_replay.py --strategy ignition --all-months
```
Requires QuestDB at http://localhost:9000.

## Parity gates (Phase 1) — ALL PASS ✅
- **A (exact):** online `str/dir/pxc/avol` == precomputed `claude_sec_feat` columns (bit-for-bit).
- **B (ignition):** oracle **+990.6 vs +1004** (1.3%), all 4 months +.
- **C (zones):** oracle **+$47,502 vs +$47,502** (exact; FADE/BREAK/FLIP = +17.5k/+5.2k/+24.8k).
- **D (flow):** oracle **+811.3 vs +814** (0.3%), all 4 months +.

Run them: `pytest tests/parity --run-parity -s` (oracles are fast; `tools/ref_eval.py`,
`gen_states.py` regenerate the ignition inputs).

### Oracle vs engine (important)
The research headline numbers are **per-signal / per-unit evaluations** (independent forward
P&L, look-ahead-corrected). Each strategy therefore has two forms:
- an **oracle** (`*_oracle.py`) that reproduces the research number — the **parity gate**;
- a live, event-driven **Strategy** (`ignition.py`, `flow.py`, `zones_strategy.py`) that applies
  the SAME decision logic in a realistic **single-position** portfolio and reports a lower,
  deployable number (ignition ~+584 pts; zones ~+$4.4k ESM5; flow position-sized).

The oracle proves logic-faithfulness; the Strategy is the object that runs live (only the Feed/Broker
adapter changes). Run the engine forms via `tools/run_replay.py --strategy ignition|flow|zones|opendrive`.

## Fourth sleeve: open-drive continuation (2026-07 research)
`strategies/open_drive.py` — at 10:00 ET enter in the direction of the 9:30→10:00 move; stop = 1.0×
morning-range (floor 5), trail = 1.5× morning-range (floor 8), flat at 16:00. Price-only (no book/flow
data), one decision/day. Evidence on the 1-second path (64 sec-covered days): **+815 pts, all 4 months
positive, both contracts positive, daily ρ = −0.01 vs the ignition sleeve**; ESM5 Mar 20–31 holdout
+154 pts. Known limits (be honest): t≈1.6, April-heavy convexity profile, fixed-point stops fail on
the 1s path (hence range-scaled), strict $-risk budgeting deletes the edge (it lives on wide-range days).
Parity gate: `tests/parity/test_parity_opendrive.py`.

## Fifth sleeve: IBS daily mean-reversion (SWING — holds overnight)
`strategies/ibs_swing.py` — at 15:59 ET: buy MOC when the day closes in the bottom 20% of its
range (IBS<0.2) while flat; sell MOC when it closes in the top 20% (IBS>0.8). No intraday stop
(classic spec) — tail risk is handled by SIZING (worst trade −347pt, 1-lot maxDD −$24.5k over
16y; see `strategies/ibs_oracle.py`). Evidence: ES=F 2010→2026 n=435, +4644pt net, t=+4.2,
win 70%, H2>H1 (no decay); whole 3×3 parameter plateau t≥4.1; SPY confirms t=4.4. Optional
GEX sizing (`gamma=GammaRegime()`): 2 lots in long-gamma regimes (win 78%, worst −160) vs 1 in
short-gamma (worst −347) → +72% total at slightly better return/DD. Parity:
`tests/parity/test_parity_ibs.py` (needs `tools/fetch_daily.py` first).

## Live path
`LiveEngine` (`core/live_engine.py`) drives the same strategies async off N `NinjaTraderFeed`s +
`NinjaTraderBroker`s (one lane per instrument) over local JSON sockets (`adapters/protocol.py`);
per-second `BookFlow` is aggregated from the live DOM (`features/bookflow.py`). The C# relay is in
`bridges/ninjatrader/`. Validated end-to-end with fake sockets in `tests/test_live_loopback.py`
and `tests/test_live_multilane.py` (no NT8 needed). Operations: `GO_LIVE.md`.

The frozen HMM (`config/hmm_es_1h.json`) and per-hour states (`config/hmm_1h_states.json`,
regenerated by `tools/gen_states.py`) come from `D:\Trading\engine_reference\`; states
match the reference sample exactly. See `D:\Trading\strategy_lab\*.md` for the specs.
