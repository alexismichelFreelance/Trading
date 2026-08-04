# Gamma-exposure (GEX) pipeline for ES (and NQ)

Goal: turn daily index option chains into per-day **gamma levels** (zero-gamma flip, call wall,
put wall, long/short-gamma regime) and test whether the future respects them intraday.

## Live daily collection (the current pipeline)
The scheduled task `Trading_GEX_Daily` (Mon-Fri 15:00 local, `engine/tools/daily_gex.cmd`) runs:
1. `tools/fetch_gex.py` — SqueezeMetrics aggregate SPX GEX/DIX → `claude_gex`
   (feeds GammaRegime, consumed per-strategy by the `*_gex` roster variants — ES).
2. `tools/fetch_cboe_gex.py` — CBOE delayed chains with TRUE OI for **SPX and NDX**
   → flip / call wall / put wall per underlying → `claude_gex_levels`
   (`underlying` column: SPX rows map to ES, NDX rows to NQ; pre-NDX rows are
   NULL = SPX). Raw chains archived in `raw_cboe/`.

`run_live --gex-levels` draws each lane's walls from its own underlying
(`instruments.yaml gex:`). **NQ status:** levels are forward-collected from
2026-07-20; the NDX→NQ basis is UNCALIBRATED (set `gex.basis` after measuring
`NQ_close - NDX_close`), and there is NO validated NQ wall/regime history yet —
observation only. A future NQ regime gate can be built from the accumulating
`total_gex` history (underlying='NDX') exactly like the ES percentile gate.

## Step 1 — get the data (you)
1. OptionsDX → SPX Option Chains → **End of Day** frequency → year **2025**.
2. Download the monthly files for **Feb, Mar, Apr, May 2025** (covers both ESH5 and ESM5 tapes).
3. Put the CSVs in `D:\Trading\gamma\raw\`.

Caveats:
- Free OptionsDX has **greeks but no open interest** — the pipeline weights gamma by *volume*
  as an OI proxy (fine for near-dated SPX, weaker than true OI).
- If the free download doesn't actually offer 2025, tell Claude — we switch source.

## Step 2 — build levels (Claude/you)
```
python build_gex.py
```
Produces `gex_levels.csv`: one row per session with
`sess_date, spot, zero_gamma, call_wall, put_wall, net_gamma_sign, total_net_gamma`.

## Step 3 — ingest + backtest (Claude)
Claude ingests `gex_levels.csv` into QuestDB and tests against the ES tick tape:
- Do **call_wall / put_wall** act as resistance / support (rejection, pin)?
- Does **zero_gamma** separate mean-reverting (above) vs trending (below) behaviour?
- On **short-gamma days** (`net_gamma_sign = -1`) is realized intraday range / trend-persistence
  higher than on long-gamma days? (the core gamma hypothesis)

## Interpretation cheat-sheet
- **Long gamma (above flip):** dealers hedge *against* the move → price pins / mean-reverts,
  range compresses. Fade extensions toward the walls.
- **Short gamma (below flip):** dealers hedge *with* the move → moves accelerate, breakouts
  run. Trade momentum / breakouts; the validated ignition strategy should do better here.
