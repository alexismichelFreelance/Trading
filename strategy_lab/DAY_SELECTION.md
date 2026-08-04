# Day-selection — how the user modulates aggression (task 25)

Question: the user trades nearly every day, so "selection" is really *intensity*.
Is the intensity CAUSAL and systematic — and does the July 9 stand-down (2 fills)
generalise? Morning-conviction proxy = fills before 10:30 ET (exact NT8
timestamps; unlike total fills it does not grow endogenously as a trade works).
186 user ES days, 2025-07-16 … 2026-07-10.

## Finding 1 — the stand-down is regime-based and systematic
Morning conviction collapses in short-gamma regimes:

| prior-day regime | days | median early fills | median early peak | stand-down rate (≤2) | median day $ | win% |
|---|---|---|---|---|---|---|
| gexLOW (short-gamma) | 51 | **2** | 1 | **59%** | 0 | 39% |
| gexMID | 66 | 11 | 10 | 38% | 0 | 45% |
| gexHIGH (long-gamma) | 69 | **13** | 12 | 32% | +163 | 52% |

The user **presses hard in long-gamma (their edge regime) and stands down in
short-gamma (their tail-risk regime).** July 9 (short-gamma, 2 fills) is not an
anomaly — it is the *modal* short-gamma day. This is learned day-selection: engage
where dip-buying reverts, pull back where it gets amplified.

## Finding 2 — it is not merely a volatility proxy
Short-gamma correlates with bigger prior-day moves (rank-corr gexp vs prior|ret|
= −0.21), and the user also stands down after big prior moves (early_fills vs
prior|ret| = −0.18). But the regime link **survives** controlling for prior
volatility: partial rank-corr(early_fills, gexp | prior|ret|) = **+0.17**. So two
largely independent drivers both say "stand down in turbulent / short-gamma
conditions." The user trades by price/volume/VWAP/pivots (not gamma), so they are
almost certainly reading the *behaviour* that short gamma produces — operationally
the rule is identical.

## Finding 3 — conviction does not finely predict P&L; over-pressing has a fat tail
rank-corr(early_fills, day_pnl) ≈ 0.00. But by conviction tercile:

| morning | days | median day $ | mean day $ | win% |
|---|---|---|---|---|
| quiet AM | 62 | 0 | +672 | 39% |
| **mid AM** | 62 | +62 | **+5,052** | 50% |
| heavy AM | 62 | +456 | **−317** | 50% |

Moderate engagement is best; **heavy pressing has a negative mean** despite a
positive median — the big losses come from over-committing. Engaged days (>2 early
fills) run median +$925 vs $0 for stand-down days, and open on up-gaps after
calmer prior sessions (gap +3.9, prior|ret| 35.9) vs stand-down days (gap −4.5,
prior|ret| 44.0).

## Pre-registered selection rule for the dip-buy sleeve (task 26)
Causal (all knowable pre-open), the exact **complement** of the validated trend
gate:
1. **Engage when gexp_prev > 1/3 (mid + long gamma); skip short-gamma
   (gexp_prev ≤ 1/3).** The user engages across mid AND high gamma (median 11 and
   13 early fills) and stands down only in short-gamma (median 2). So the gate is
   the mirror of the trend gate (trend ON ≤ 1/3, dip-buy ON > 1/3), not a
   high-only gate — and it catches both anchor days (2026-07-08 gexp 0.37,
   2026-07-10 gexp 0.61, both mid).
2. **Cap aggression** — model the "mid AM" band, not "heavy"; over-sizing is where
   the left tail lives. A hard per-day size cap, not a conviction ramp.
3. **Optional damp after big prior moves** (prior|ret| high) — a secondary
   stand-down tell, independent of regime.

Not deployed. This gates the sleeve; entries remain the band-retest / put-wall
levels (BAND_RETEST_STUDY.md, GEX_LEVELS.md). Thresholds are in-sample and ride
the same late-Aug forward test as the rest.
