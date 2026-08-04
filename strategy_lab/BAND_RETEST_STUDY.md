# Forming-base / band-retest study — mechanizing the user's demonstrated trade (2026-07)

**Origin:** the user's two live wins (2026-07-08 +525pt, 2026-07-10 +281pt manual, sized).
Method: anchor-tested mechanization — a spec that fails to catch the motivating trades is
mis-specified regardless of P&L.

## Round 1 — "forming-base at the flush low": MIS-SPECIFIED
Flush→base-holds→enter-at-base (F×M grid, both datasets): tiny samples (n=3–22), small
positives, one 2026-negative cell — and **the anchor test failed in every cell** (neither actual
entry caught). The user does NOT buy the base at the extreme. Spec discarded.

## The decode — the user's entries are their TOOLKIT, to the tick
From the recorded bars at their actual fill times:
- **07-08 11:02 ET @ 7492.5** = VWAP−2σ touch (7489.9, +2.6pt), 11 min after a local flush low,
  S2 overhead. Scale-outs 7504–7510 vs VWAP 7508.
- **07-10 10:53 ET @ 7586.9** = **prior-day close 7586.8 (+0.1pt)** in confluence with VWAP−1σ
  (+0.7pt), 19 min after the morning low.
The trade = *first touch of a lower VWAP band (or band-confluent prior level) after an intraday
flush, scaling out into VWAP*. This decode stands on its own: the user's discretionary entries
are specifiable levels — what's NOT yet specified is their selection layer (which days, which
touch, when to stand down — e.g. their July 9 no-trade).

## Round 2 — "band/prior-close retest fade": REGIME-SPLIT, then the regime story failed
Rule: after ≥15pt decline (last 90m, ≥10m past the low), buy the first touch of VWAP−2σ (setup
A) or prior close when band-confluent (setup B); stop −6, scalp +4→BE, runner to VWAP, 120m/MOC.
Anchor test: 07-10 caught to the tick (B); 07-08 caught as the same family (A) but 12pt above
the user's fill — half-passed.

| dataset | n | win | total | months+ |
|---|---|---|---|---|
| 2026 recorded (18d) | 13 | 85% | **+33.2pt** | 2/2 (setup A 8/8) |
| 2025 research (72d) | 43 | 56% | **−39.6pt** | 0/3 |

**Gamma conditioning does NOT rescue 2025:** short-gamma −12.1 / mid −15.3 / long-gamma −12.2 —
the rule lost in every GEX bucket, so "works now because mid-gamma" is unsupported. Whatever keys
the 2026 profitability is not the GEX percentile.

## Honest verdict
- **Not deployable.** The 2026 positive is 13 trades over 18 days AND is contaminated (the two
  anchor days that inspired the spec are inside the sample). The 2025 negative is broad.
- **What survives:** the decode. The user's edge expresses at nameable levels (VWAP bands,
  prior close, pivot S-levels) — the machine can now DRAW their trade (bands + confluence
  already on the chart) even though it cannot yet SELECT like them.
- **Forward plan (pre-registered):** freeze this spec unchanged; re-evaluate on NEW recorder
  days only (excluding 06-16..07-10), after ≥6 more weeks of data. If the fresh-sample stats
  hold near the 2026 numbers, revisit; if not, the 2026 result was luck+contamination. No
  parameter changes in between (that would restart the mining clock).

Scripts: `strategy_lab/forming_base.py` (round 1), `band_retest.py` (round 2 + decode inputs).
