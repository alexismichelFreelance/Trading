# DipBuy sleeve — the user-modeled long-gamma mean-reversion sleeve

Ties together the three research threads that pointed at it:
- **What** to trade: the band-retest entry (BAND_RETEST_STUDY.md) — after an
  intraday flush, buy the first touch of VWAP−2σ (setup A) or a band-confluent
  prior-day close (setup B), scale half at +4, run the rest to VWAP. Shorts
  mirror the upper band.
- **When** to trade it: the regime gate (GEX_FINDINGS.md, DAY_SELECTION.md) —
  ON when gexp_prev > 1/3 (mid + long gamma), the exact complement of the trend
  gate; OFF in short-gamma, the user's stand-down regime and the sleeve's
  tail-risk regime.
- **Where** price reverts: the gamma levels (GEX_LEVELS.md) — the put wall as a
  covering floor in long gamma (a future entry anchor once the sample matures).

## Implementation
`engine/engine/strategies/dip_buy.py` — causal online sleeve on 1m bars: session
VWAP/σ cumulative from the RTH open, backward-only flush window, scale/stop/VWAP/
MOC management, moderate fixed size (RISK $1,000, cap 5 — the "mid AM" band, not
"heavy", per DAY_SELECTION.md). Regime is the engine's RegimeGate (MR_SLEEVES);
size safety is the RiskSupervisor. `run_live --strategies dipbuy --gex-gate`.

## Anchor test — PASSES (strategy_lab/dip_anchor.py)
Run ungated over the 2026 recorded 1m bars, 14 entries / 22 days (matches the
study cadence), and it catches both motivating trades:
- **2026-07-08**: long @7504.7 (setup A, VWAP−2σ) vs the user's 7492.5 — same setup.
- **2026-07-10**: long @7586.75 (setup B, prior-close) vs the user's 7586.9 —
  **to within 0.15pt.**

## Status — built, NOT yet a validated edge
The 2026 band-retest sample was small and contaminated by these very anchor days
(BAND_RETEST_STUDY.md: +33pt 2026 vs −39.6pt 2025). This sleeve therefore
**deploys OFF by default** and rides the pre-registered late-Aug forward test on
fresh recorder days. What is delivered now is a clean, anchor-tested, regime-gated
mechanism with 7 unit tests — ready to paper-trade the moment the forward numbers
justify it. Parity untouched (new sleeve, no gate).
