# Go-live (paper) runbook

The engine is safe to run daily in paper (Sim101) alongside manual trading:
per-strategy attribution, the RiskSupervisor (in-flight vetting, caps, rate
limit, kill switch, EOD flatten), the GEX regime gate, and the one-chart overlay.

## Paper vs live model
EVERY strategy + variant ALWAYS paper-trades (visible signals, own book, never
sent to the broker, never risk/regime gated — you see the raw strategy). Only the
`--live` subset ALSO routes to the NT8 broker. Paper fills paint in muted cyan
(`~tag`); live fills paint green/red.

## Daily session command
```
cd D:/Trading/engine
.venv/Scripts/python.exe tools/run_live.py \
    --paper all \
    --live zones,dipbuy \
    --gex-gate --gex-levels \
    --record \
    --panel bottomleft
```
- `--paper all` — the full roster paper-trades: ignition, ignition_fixed,
  opendrive, flow, flow_fixed, zones, zones_gap, dipbuy, ibs, ibs_gex. Every
  signal (incl. ignition) is visible on the one chart.
- `--live zones,dipbuy` — only these route to NT8. Empty = pure paper/observation.
  The GEX gate + risk limits apply to the LIVE subset only.
- End-of-session prints per-paper-sleeve realized pts + net position.
- `--gex-gate` — trend sleeves take entries only when gexp_prev<=1/3 (validated).
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
# first look — UNGATED so it fires regardless of today's regime:
.venv/Scripts/python.exe tools/run_live.py --strategies zones,dipbuy --gex-levels --record
# real behavior — gated to gexp_prev>1/3 (stands down on short-gamma days):
.venv/Scripts/python.exe tools/run_live.py --strategies zones,dipbuy --gex-gate --gex-levels --record
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
