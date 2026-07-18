# ES Strategy #2 — VWAP-Reversion Fade  ⚠️ FAILS 1-SECOND VALIDATION

> **CRITICAL (2026-06):** All performance below was computed on **1-minute bars**. When the exact
> same entries are filled on the **1-second tape** (ESH5, n=429), every exit scheme is flat-to-
> negative: trail-4 **−320** (vs +428 minute), trail-8 −181, trail-12 −221, fixed3+target −79,
> fixed6+target −33, fixed10+target −28, fixed6-no-target +57 (≈ noise, +0.13/trade).
> **The minute-bar edge (+236 fixed, +807 trailing) was a fill artifact** — the ~1pt/trade edge is
> destroyed by intra-minute noise + bid/ask bounce + cost. NOT tradeable as-is.
>
> **Update 2 — ESM5 1s replication (decisive):** the one surviving 1s config (deeper 2.5σ + wide
> fixed 8pt stop, no target) was +136 on ESH5 but **−170 on ESM5**. Per-month exposes it: deeply
> negative in **April (−339, the crash/trend month)**, positive in **May (+169, balanced)**. At
> execution resolution the reversion is a **balanced-regime-only edge that trend months destroy** —
> no robust standalone edge. Tradeable only with a reliable trend-day exclusion, which can't be
> validated on ~65 days. Idea kept OPEN per no-give-up; next lever = a balanced-regime gate and/or
> limit-order entry. Everything below is the minute-bar dev record, NOT execution-validated.
>
> **Update 4 — FINAL shape (1s execution + level study).** Daily extremes cluster at the OPEN and
> at ~2σ from VWAP (level study). Gating the fade to the **first hour** ~doubles ESM5 edge:
> **+1.65/trade, +336, both Apr & May positive, n=203** at 1s with limit-join fills. Round-number
> gating does NOT help (magnet ≠ profitable fade level). **February-2025 unfixable** through 6
> filters (chop/slope/range/session-eff/first-hour/round) — a directional-grind regime where fading
> loses. KEEPER config: first-hour |z|≥2 VWAP fade, limit-join entry (cost 0.27), 6pt stop, exit at
> VWAP. Real edge 3 of 4 months; stand aside in one-way grinds. Banked into the ignition portfolio.
>
> **Update 3 — gates restored + limit entry, 1s execution (the real test).** Best config =
> ungated, |z|≥2, **6pt stop + VWAP target (bank the bounce), limit-JOIN entry** (save a tick;
> cost 0.27). At 1-second resolution:
> - ESM5 (Apr–May): **+257** (Apr +181, May +76) — genuinely positive, execution-validated.
> - ESH5 (Feb–Mar): **−50** (Mar +151, **Feb −201**).
> - So positive in **3 of 4 months**; February-2025 is a tail-loss month.
> Key mechanics confirmed at 1s: (a) VWAP *target* >> ride-to-timeout (banks the reversion before a
> trend resumes — this is what made April positive); (b) **limit-join entry adds ~+0.24/trade** vs
> market (the tick matters); (c) **passive** limit (1 tick better, wait for fill) LOSES to adverse
> selection on a fade. **Unsolved:** no regime filter (chop / slope / range / session-efficiency)
> fixes February without breaking April — the two volatile quarters want opposite gating. Edge is
> real but carries an un-filtered regime-tail (Feb-type directional grind). Options: multi-day/
> external (VIX) regime detector, or accept + size down. Idea remains OPEN.

---

# ES Strategy #2 — VWAP-Reversion Fade (minute-bar dev record)

A mean-reversion strategy — the opposite mechanism to the ignition (momentum) strategy. Fades
over-stretched price back to session fair value (VWAP). Developed by killing the fat-tail losers
rather than abandoning the idea.

## Mechanism (per 1-minute RTH bar, `claude_rth_feat`)
Fields: `sess_vwap` (session VWAP), `sess_sigma` (running std of price around VWAP), `z=(c-vwap)/sigma`,
`er` (efficiency ratio = regime proxy).

**Entry (fade):**
- Stretched: `|z| >= 2.0` AND price at least `MIND = 2.0 pt` from VWAP.
- Regime: only in balance — `er < 0.40` (chop).
- Direction: `z<=-2` → long (price below VWAP, expect snap up); `z>=+2` → short.
- One position at a time; no new entries after minute 372 of the session.

**Exit — TRAILING (the breakthrough):**
- **Initial hard stop = 3 pt** (caps the rare runaway from −81 to −3.5 worst).
- **Trailing stop = 4 pt** behind the max favorable excursion. NO fixed target — let the reversion
  run *through* VWAP and trail out. (Reversion overshoots fair value → becomes momentum; the trail
  harvests it. This more than tripled P&L vs exiting at VWAP.)
- Time stop: 60 min.
- NO slope filter, NO regime-flip bail, NO VWAP target — the trailing stop made all three redundant.

Cost 0.517 pt/round-turn included.

## Performance (in + out of sample, both contracts) — TRAILING exit
| month | total pt |
|---|---|
| ESH5 Feb (OOS) | +34 |
| ESH5 Mar (OOS) | +119 |
| ESM5 Mar | +41 |
| ESM5 Apr | +420 |
| ESM5 May | +193 |
| **Total (267 trades)** | **+807  (+3.02/trade)** |

Win ~46% (positive-skew: small losers, big runners; top trades +44 to +56; worst single −3.5).
Positive in all 5 month-buckets, both contracts, in and out of sample.

### Why trailing changed everything (per "always test a trailing stop")
- Fixed 3pt stop + VWAP target: **+236**.
- Trailing 4pt, no target: **+807** — 3.4×. Removing the VWAP target captures the overshoot past
  fair value; the trailing stop (not a fixed slope filter) handles the "fade into a trend" risk by
  stopping wrong fades small and letting right ones run.
- Robustness: trail 3/4/5/6 all give +766…+809 (flat — not a tuned knife-edge). Dropping a
  parameter (slope filter, regime-flip) *raised* P&L = anti-overfit.

### Prior (fixed-stop) version, for the record: +236 over 191 trades, all months positive.

## How it was found (methodology that worked)
1. Raw fade-to-VWAP: **−116, fat-tailed** (worst trade −81). Phenomenon real (chop stretches touch
   VWAP 75%) but unprofitable.
2. **Characterized the fat tails** instead of quitting: worst-decile trades had session range-so-far
   138pt vs 67pt, dist-from-open 44 vs 22 → disasters are fades on big-range / trending days.
3. **Hard stop** (cap the tail): 8pt → +105; tighter is better; 3pt → +353 (4/5 months).
4. Remaining hole = **February** (2 bad days, losers all "regime-flip" = fade caught at a trend start).
5. **VWAP-slope filter** (don't fade against the trend) → Feb neutralised, all 5 months positive.

## Honest caveats (NOT deployable yet)
- **Trailing exit fill is idealized** — simulated on the real minute-by-minute path (ordered, better
  than the old MAE approximation), but assumes the trail fills at `maxFE − 4`. Intra-minute slippage
  needs a 1-second-tape stop sim to confirm (less severe than the old 3pt scalp since the trail is
  wider and winners are multi-point).
- **April (high-vol month) ≈ half the total** (+420 of +807). The edge is largest in volatile
  regimes; calm months are positive but modest (Feb +34, ESM5-Mar +41). Position-size by vol.
- **Overfit risk is LOW but nonzero**: trail 3–6 all ≈ +800, dropping params raised P&L, holds across
  2 contracts + 5 months. Remaining tuned knobs: chop-th (0.40), ZIN (2.0), MIND (2.0) — untested for
  sensitivity / whether the chop gate is even needed with the trailing exit.
- ~3–4 trades/day, 267 total — moderate sample. Needs 1-second fill model + more OOS before live.

## Open hardening / edge-push (in progress, per no-give-up)
1. Validate the 3pt-initial / 4pt-trail on the 1-second tape (replace minute-path idealization).
2. Test removing the chop gate (does the trail make it unnecessary, like the slope filter?).
3. Sensitivity of chop-th / ZIN / MIND.
4. Daily-P&L correlation vs the ignition strategy (confirm it's a true diversifier).

## Why it complements the ignition strategy
Opposite mechanism (fade vs chase), opposite regime (chop vs trend — the ignition strategy's HMM gate
routes trend days to momentum; this one harvests the chop days). Likely **low/negative correlation** —
a genuine second strategy, not a variant. Next: measure correlation of daily P&L between the two.
