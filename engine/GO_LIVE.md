# Go-live (paper) runbook

The engine is safe to run daily in paper (Sim101) alongside manual trading:
per-strategy attribution, the RiskSupervisor (in-flight vetting, caps, rate
limit, kill switch, EOD flatten), the GEX regime gate, and the one-chart overlay.

## Daily session command
```
cd D:/Trading/engine
.venv/Scripts/python.exe tools/run_live.py \
    --strategies zones \
    --gex-gate --gex-levels \
    --record \
    --panel bottomleft
```
- `--strategies zones` — start with the sleeve that matches the user's regime
  (long-gamma mean reversion). Add `ignition,opendrive` once the GEX gate has a
  live track record; they are trend sleeves and only fire on short-gamma days.
- `--gex-gate` — trend sleeves take entries only when gexp_prev<=1/3 (validated).
- `--gex-levels` — draw put wall / call wall / flip as S/R lines (eyeball the
  put-wall line vs price on the first session; recalibrate `--gex-basis` if off).
- `--record` — grow claude_bars_live (feeds every frozen forward test).
- Risk limits are ON by default: sleeve cap 10, gross cap 15, 4 orders/5s,
  halt at -$5,000, entry lockout 15:45 ET, EOD flatten 15:58 ET.

Pre-market: run `tools/fetch_cboe_gex.py` (or the scheduled task) so the day's
gamma levels + regime are current.

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
