# Quiet-classics screen — old, discreetly-published ideas run through our system (2026-07)

**Ask:** find low-noise ideas (old quiet publications, no ICT-style marketing) incl. swing, and
verify the numbers. **Data:** Track A = 16y daily SPX OHLC (Yahoo ^GSPC, 3,900 days, 2010→2025-07)
— unlocked by needing only daily bars; Track B = our 72-day ES tape. Costs noted (0.517pt RT ES;
swing trades amortize it to noise). Sub-period split 2010-17 / 2018-25 guards decay.

## Track A — swing classics on 16 years (buy&hold drift baseline: +1.31 pt/day)

| idea (source) | exposure | mean/day in-mkt | t | H1 | H2 | verdict |
|---|---|---|---|---|---|---|
| **A2 IBS** <0.2 buy close, >0.8 sell (UBS desk note lineage; "IBS effect" paper) | 36% | **+3.98** | **+3.7** | +1,158 | **+4,392** | **SURVIVOR — strongest; no decay (H2 > H1)** |
| **A1 RSI(2)** <10 above 200dMA (Connors 2008) | 11% | +3.63 | +2.2 | +394 | +1,155 | **survivor** |
| A2b IBS + 200dMA filter | 28% | +2.35 | +2.6 | | | filter hurts (cuts crash rebounds) |
| A3 Turn-of-month (Lakonishok-Smidt) | 33% | +1.39 | 1.5 | | | ≈ drift — decayed, dead |
| A4 Overnight-only drift (Cooper "night moves") | 100% | +0.65 | 2.6 | | | ≈ half of drift; no edge vs holding since 2010 |
| A5 DIX ≥0.8 next-day long (SqueezeMetrics) | 20% | +1.56 | 0.9 | | | ≈ drift over 15y; low-DIX days also fine → **our 70-day DIX result was regime luck — corrected** |

Survivors both mean-revert at daily scale and overlap (weak close ≈ low RSI2); treat as ONE
family. Per-trade: IBS ~456 trades, ~3.1-day hold, ~+12pt/trade gross vs 0.52 cost.

**ES-window translation check (honesty):** on OUR Feb–May 2025 tape, IBS<0.2 (n=12) → next-day
**−15.5pt**: the screen's known weakness — IBS bleeds in momentum-crash regimes — landed exactly
in our window. The 16y record includes 2018Q4/2020/2022 and still prints t=3.7, H2>H1. Net read:
real, but a crash-regime *loser* → genuine diversifier against our trend sleeves (which earn
exactly then), and it must be sized for overnight gap risk (a different risk class than the
intraday book).

## Track B — intraday quiet classics on our 72-day ES tape

| idea | best variant | n | result | verdict |
|---|---|---|---|---|
| Crabel ORB (+stretch) | OR15 +10% stretch, stop far side, MOC | 72 | +947pt, t=1.7, 78% from April | **redundant: corr +0.85 with our open-drive sleeve, ~same total ($39.0k vs $41.4k)** — same trade, worse (stop-chase) entries |
| Crabel NR7 filter | OR30 after NR7 day | 16 | +14.5pt | no effect here (tiny n) |
| Crabel inside-day filter | OR30 | 7 | −127pt | negative (tiny n) |
| Market Profile "80% rule" (Dalton/CBOT) | open outside prior VA, re-entry → traverse | 17 | traverse rate **59%** vs claimed 80%, t=0.9 | claim not confirmed on this window |

The ORB finding is satisfying: two independently-derived expressions of morning momentum
(Crabel 1990 stop-breakout vs our 10:00 drift-follow) converge on the same P&L stream —
open-drive already owns that slot with cleaner mechanics.

## Multiple-testing ledger
Track A: 6 ideas tested, 2 survive; IBS's t=3.7 survives Bonferroni ×6. Track B: ~10 configs,
0 new edges claimed (only a redundancy identification). All negatives reported.

## Recommendation
Build the **daily mean-reversion swing sleeve (IBS family, RSI-2 as confirm)** as the book's
5th sleeve — the only genuinely NEW, uncorrelated risk found: multi-day holds, MOC execution,
crash-regime loser (sizes must respect overnight gap risk; consider the GEX long-gamma
percentile as a *size-up* filter since pinning regimes favor mean reversion). Verify next on
ES daily data (longer than our 72d — e.g., free continuous ES or SPY OHLC 16y) with gap-aware
stops before any engine build.
