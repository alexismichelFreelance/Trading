# NDX GammaRegime Implementation — Summary

## Changes Made

### 1. Added `NDXGammaRegime` class to `engine/features/gamma.py`
- Reads from `claude_gex_levels` (CBOE true-OI, ≤7 DTE)
- Computes trailing 252-day percentile of `total_gex` (same causal methodology as SPX GammaRegime)
- Staleness checking with 4-day max age (same as SPX)
- Atomic snapshot reload for thread-safety

### 2. Updated `_gamma_or_none()` in `tools/run_live.py`
- Now returns `NDXGammaRegime` for NQ root symbol
- Returns `GammaRegime` (SPX) for ES root symbol
- Returns `None` for other instruments (GC, etc.)
- Separate cache entries per underlying

### 3. Updated `TrendJoinStrategy` in `engine/strategies/trend_join.py`
- Accepts `gamma` parameter in `__init__`
- Calls `super().__init__()` to inherit `BaseStrategy.gamma`
- Added `gamma_entry_ok` check before entry (both on armed pullback and direct confirmation)
- TrendJoin is a **continuation sleeve** → wants `short` gamma (dealers amplify)

### 4. Added trendjoin support to `tools/run_replay.py`
- Added `"trendjoin": False` to `NEEDS_BARS`
- Added `make_strategy` branch for `trendjoin` using instrument-specific params from `instruments.yaml`

## NDX Gamma Data Status
- **Source**: `tools/fetch_cboe_gex.py` (CBOE true-OI daily fetch)
- **Tables**: `claude_gex_levels` (daily flip/walls/regime), `claude_gex_strikes` (per-strike curves)
- **History**: 29 sessions (2026-07-20 → 2026-09-08)
- **Current regime (2026-09-08)**: Long gamma (NDX total_gex = +1.70B, local_sign = -1)

## SPX vs NDX Regime Correlation (11 overlapping days tested)
| Date | SPX | NDX | Agree |
|------|-----|-----|-------|
| 2026-08-25 | Long | Long | ✓ |
| 2026-08-26 | Long | **Short** | ✗ |
| 2026-08-27 | Long | Long | ✓ |
| 2026-08-28 | Long | Long | ✓ |
| 2026-08-31 | **Short** | Long | ✗ |
| 2026-09-01 | Short | Short | ✓ |
| 2026-09-02 | Short | Long | ✗ |
| 2026-09-03 | Long | **Short** | ✗ |
| 2026-09-04 | Long | Long | ✓ |
| 2026-09-05 | Long | Long | ✓ |
| 2026-09-08 | Long | Long | ✓ |
| **Agreement** | | | **73%** |

## Next Steps (To Validate)

1. **Run replay comparison** for NQ trendjoin variants:
   ```bash
   .venv/Scripts/python.exe tools/run_replay.py --strategy trendjoin --symbol NQ --all-months
   .venv/Scripts/python.exe tools/run_replay.py --strategy trendjoin --symbol NQ --all-months --gamma-regime
   ```
   (Need to add `--gamma-regime` flag to run_replay.py or create a test that passes gamma)

2. **Measure**: 
   - Total P&L with vs without gamma gate
   - Max drawdown reduction
   - Win rate by regime
   - Sessions traded (gate should fire less on long-gamma days)

3. **If conclusive** (≥20% DD reduction, <10% P&L sacrifice like ES 96%/27%):
   - Add `trendjoin_gex` to NQ `live:` roster in `config/live.yaml`
   - Consider gating other NQ trend sleeves (opendrive, etc.)

4. **Extend to other NQ sleeves**:
   - `opendrive_gex` (already in live.yaml, needs gamma)
   - `flow_gex` (needs NDX gamma)
   - `ibs_gex` (already accepts gamma, will now get NDX)

## Architecture Notes
- Gamma gating is a **STRATEGY CHOICE**, not an engine gate (per BaseStrategy design)
- Raw and _gex variants run side-by-side in paper; user picks which routes live
- Fail-open: if gamma data stale/unavailable, `_gex` variant behaves as raw
- This is exactly the pattern already proven on ES (GEX_FINDINGS.md D)