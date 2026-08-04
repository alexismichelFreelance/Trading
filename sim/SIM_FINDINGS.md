# Portfolio Simulation — Findings (READ THIS BEFORE TRUSTING ANY BACKTEST $)

Event-driven sim of the 2-strategy portfolio (ignition + supply/demand zones) on a $100k account,
2% risk/trade, NinjaTrader-Free costs ($5.28 round-turn commission+fees), with position sizing,
scale-out (½ at +1R), stop→breakeven, and margin cap. Engine: `portfolio_sim.js`.

## Slippage model matters enormously — and must be REGIME-coherent
A first "vol-scaled" pass keyed slippage off the 10s window around each ignition. But an ignition is
itself a volatility spike, so that charged ~2-4 ticks on EVERY fill (calm months too); removing April
then double-penalized the calm months and falsely showed −7.7%. **Bug.** Fixed by tying slippage to the
prevailing REGIME (prior 5-min realized range), so calm days get ~2 ticks and the crash ~3.

| scenario | net P&L | return | max DD | profit factor |
|---|---|---|---|---|
| 1-tick slippage (naive, too optimistic) | +$277,206 | +277% | 10.7% | 1.40 |
| ignition-window slippage (BUGGED, too harsh) | +$96,544 | +96.5% | 30.3% | 1.12 |
| **REGIME-coherent slippage** | **+$191,297** | +191% | 17.6% | **1.26** |
| **REGIME-coherent, EX-APRIL** | **+$64,781** | +64.8% | 23.1% | **1.14** |

**Per-month net $ (regime-coherent slippage; avg slip Feb 0.47 / Mar 0.58 / Apr 0.72 / May 0.40 pt):**
April +$126,515 · Feb **+$8,393** · Mar **+$36,329** · May **+$20,059** — **all four months positive.**

## What this means (corrected)
- There IS a modest normal-market edge after realistic costs: ex-April +64.8%, **profit factor 1.14**,
  positive in every month. The earlier "no edge / loser ex-April" was a slippage-model bug, not reality.
- **BUT April is still ~66% of the P&L** ($126k of $191k) — real concentration risk; don't annualize.
- **Ex-April PF 1.14 is THIN** — sensitive to the slippage assumption. If calm fills are worse than the
  ~2 ticks modeled, it erodes toward 1.0. The honest descriptors: PF ~1.1-1.3, ~18-23% max DD, modest.

## Caveats that survive the correction
- The sim used a generic fixed-6pt-stop + scale-out overlay; the validated BOOK / zone exits may do
  better still in calm months, so calm-month numbers are if anything conservative on execution.
- Zones (n=32) show 100%-ish win — small-sample + scale-out construction (BE stop makes losers rare).
  Not a reliable standalone figure; the ignition leg carries the portfolio.
- Result is sensitive to the slippage assumption (calm ~2 ticks). Worse calm fills push ex-April PF→1.0.

## Honest bottom line (corrected)
The research findings (ignition tilt, virgin-zone bounce, zone-confluent entries) are REAL,
cross-validated phenomena. As a sized, realistically-costed system with REGIME-coherent slippage, the
portfolio is **positive in all four months** and makes **+64.8% / PF 1.14 even excluding the April
crash** — so there IS a modest normal-market edge, contrary to my earlier (bugged) claim. The real
limits are: **April is ~66% of P&L** (concentration), ex-April PF 1.14 is **thin** and slippage-
sensitive, and it's only 4 months / 2 contracts. Verdict: a genuine but modest edge worth forward-
testing — NOT a guaranteed money machine, NOT a dead system. Before capital: more data across regimes,
tighter fill modeling, and a live paper-trading period to confirm the calm-month edge survives reality.

## Cost / data reference (2026)
- IB ES: ~$0.85 comm + ~$1.60 fees/side ≈ $4.90 round-turn (~0.10pt).
- NinjaTrader: Free ~$5.28 RT, Monthly ~$1.98, Lifetime ~$1.18 (+$1,499 license). +exchange/NFA ~$1.60/side.
- ES: $50/point, $12.50/tick. Slippage 1 tick = $12.50; crash fills can be 2–4+ ticks.
