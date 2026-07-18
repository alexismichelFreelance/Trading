# Go-live (paper) runbook

The engine is safe to run daily in paper (Sim101) alongside manual trading:
per-strategy attribution, the RiskSupervisor (in-flight vetting, caps, rate
limit, kill switch, EOD flatten), per-strategy gamma variants, and the one-chart overlay.

## NT8 setup — TWO charts (this is why you get orders AND overlays)
A NinjaTrader **strategy** on a chart suppresses that chart's native
order/execution display; an **indicator** does not. NT8 also can't run a
strategy without a chart (Control Center's Strategies tab only *monitors*
running strategies — it can't start one). So the bridge runs on two charts:

- **Main ES chart (the one you watch):** add the **EngineOverlay** indicator
  (`Indicators/EngineOverlay.cs`). It only draws (zones / gamma / S-R / signals /
  status) over socket 36004. Indicators never hide orders, so your native
  entry/exit markers, fills, and P&L stay visible here.
- **Second ES chart (minimized, ignore it):** apply the **EngineRelay** strategy
  (`Strategies/EngineBridge.cs`) on Sim101, Calculate = On each tick. It does
  data + order routing (market 36001, broker 36002). It DOES hide orders on *its*
  chart — that's fine, you never look at it. Same instrument/session as the main.

Compile both (F5). Result: native orders AND engine overlays on your main chart.
`run_live` connects feed→36001, broker→36002, painter→36004 (all defaults).

## Multi-instrument lanes (ES + NQ + ...)
The engine is multi-instrument: one process, N feeds + N brokers, strategies
per lane, ONE RiskSupervisor (dollar-notional caps available) and one portfolio
blotter. Per added instrument you need in NT8:
- a second minimized chart of that instrument with **EngineRelay**, its
  `MarketPort`/`BrokerPort` properties set to that lane's ports (e.g. NQ
  36011/36012 — the relay exposes them in the strategy dialog now);
- (optional) that instrument's watch chart with **EngineOverlay** on the lane's
  draw port.
Lane ports + rosters live in `config/live.yaml`; contract specs ($/pt, tick) in
`config/instruments.yaml`. Then:
```
.venv/Scripts/python.exe tools/run_live.py --instruments ES,NQ --record
```
- No `--instruments` flag = the single-ES behavior above, unchanged.
- Warmup is per lane: a silent NQ feed can never keep ES from going live.
- GEX: `--gex-levels` draws each lane's own walls — ES from the SPX chain,
  NQ from the NDX chain (both collected daily by the `Trading_GEX_Daily` task).
  The NQ basis starts UNCALIBRATED (`instruments.yaml gex.basis: 0.0`) — measure
  `NQ_close - NDX_close` over the first sessions and set it, same procedure as
  the ES +52. The `*_gex` strategy variants (gamma-regime entries) stay ES-only
  until enough NDX history accumulates in claude_gex_levels to validate one for NQ.
- Dollar caps: `--max-sleeve-usd 600000 --max-gross-usd 1200000` (off by
  default; contract caps still apply).
- CAUTION: strategy thresholds (stops/targets in points) are ES-calibrated.
  Running them on NQ is for OBSERVATION/paper first — retune before any live
  routing on a new instrument.

## Paper vs live model
EVERY strategy + variant ALWAYS paper-trades (visible signals, own book, never
sent to the broker, never risk-gated — you see the raw strategy). Only the
`--live` subset ALSO routes to the NT8 broker. Paper fills paint in muted cyan
(`~tag`); live fills paint green/red.

Gamma-regime awareness is a STRATEGY property, not an engine gate: the `*_gex`
roster variants (ignition_gex, opendrive_gex, flow_gex take entries only on
short-gamma days; dipbuy_gex only on mid/long-gamma; ibs_gex sizes up in
long-gamma) run alongside their raw twins, so the paper record shows both and
you route whichever earns. ES only — SPX gamma is not an NQ signal.

## Daily session command
```
cd D:/Trading/engine
.venv/Scripts/python.exe tools/run_live.py \
    --paper all \
    --live zones,dipbuy \
    --gex-levels \
    --record \
    --panel bottomleft
```
- `--paper all` — the full roster paper-trades: ignition, ignition_fixed,
  ignition_gex, opendrive, opendrive_gex, flow, flow_fixed, flow_gex, zones,
  zones_gap, dipbuy, dipbuy_gex, ibs, ibs_gex. Every signal is visible.
- `--live zones,dipbuy` — only these route to NT8. Empty = pure paper/observation.
  Risk limits apply to the LIVE subset only.
- End-of-session prints per-paper-sleeve realized pts + net position.
- `--gex-levels` — draw put wall / call wall / flip as S/R lines (eyeball the
  put-wall line vs price on the first session; recalibrate `--gex-basis` if off).
- `--record` — grow claude_bars_live (feeds every frozen forward test).
- Risk limits are ON by default: sleeve cap 10, gross cap 15, 4 orders/5s,
  halt at -$5,000, entry lockout 15:45 ET, EOD flatten 15:58 ET.

Pre-market: run `tools/fetch_cboe_gex.py` (or the scheduled task) so the day's
gamma levels + regime are current.

## Watch the dip-buy sleeve in sim
To SEE the user-modeled mean-reversion sleeve trade (paper only — its P&L is not
yet validated, see DIP_BUY_SLEEVE.md):
```
# raw — fires regardless of today's regime:
.venv/Scripts/python.exe tools/run_live.py --strategies zones,dipbuy --gex-levels --record
# regime-aware — the dipbuy_gex VARIANT stands down on short-gamma days:
.venv/Scripts/python.exe tools/run_live.py --strategies zones,dipbuy_gex --gex-levels --record
```
Its entries/exits paint as arrows tagged `dipA/dipB-entry`, `dip-scale`,
`dip-vwap`, `dip-stop`; the panel shows its position. Preview on recorded bars:
`strategy_lab/dipbuy_preview.py` (14 trades, mechanism looks like the user's).

## Nightly scorecard
```
.venv/Scripts/python.exe tools/scorecard.py --days 20
```
Reads the NT8 db (source of truth for fills), splits engine (Name='O<n>') vs
manual, per-day realized $ with the dealer-gamma regime + gap, and FLAGS:
- `ENG LOSS` — engine daily realized <= the kill-switch budget
- `burst Nf` / `CAP N` — order-count or position spikes (the 2026-07-09 pathology;
  now prevented live by the RiskSupervisor, still flagged historically)
- `vs-user` — engine and user opposite-signed on ES with both > $500

### First read (2026-07-06..10, pre-supervisor)
The engine was `vs-user` on 4 of 5 trading days and LOST on 2026-07-10 (the
put-wall dip-buy day the user made +$38k) — concrete confirmation that the
trend-weighted engine fights the user's long-gamma mean-reversion edge. The
CAP/burst flags are all pre-supervisor; they cannot recur under the new limits.
This is why the roadmap points at the dip-buy sleeve (task 26).

## Weekly / forward checkpoints
- Recorder library grows automatically; frozen forward tests (band-retest,
  gamma levels) re-evaluate ~late Aug 2026 on post-2026-07-10 data only.
- Watch the scorecard `vs-user` rate: if it stays high, the engine's allocation
  is still mis-matched to the regime and needs rebalancing toward mean reversion.
