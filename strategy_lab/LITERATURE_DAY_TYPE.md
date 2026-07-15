# Literature review — can we classify the day (trend vs range) reliably?

Question: reliably tell a VWAP-respecting (range, fade works) day from a
VWAP-traversing (trend, fade dies) day, early enough to act. Reviewed the real
literature, then tested its causal predictors on our data.

## What the literature actually says

**1. Gao, Han, Li & Zhou (2018), "Market Intraday Momentum," Journal of Financial
Economics.** The first half-hour return predicts the last half-hour return on
SPY, 1993–2013 — statistically and economically significant, and **stronger on
high-volatility, high-volume, recession, and macro-news days.** This is the
peer-reviewed version of our own finding (the VWAP side price occupies early
persists, rho +0.45). It is a real, replicated anomaly — but a *moderate* one
(a tilt, profitable, not a clean switch).
[SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2440866) ·
[JFE](https://www.sciencedirect.com/science/article/abs/pii/S0304405X18301351)

**2. Market Profile / Dalton, "Mind Over Markets" — the Initial Balance (IB).**
The first hour's range (IB) is the practitioner's day-type tool: a *wide* IB
favours rotation (range/normal day, fade), a *narrow* IB is vulnerable to range
extension (trend or non-trend — ambiguous). Trend days are rare (**5–10% of
days**). No hard numeric thresholds are published — it is a visual/heuristic
framework. [marketcalls](https://www.marketcalls.in/market-profile/market-profile-different-types-of-profile-days.html)

**3. Crabel, "Day Trading with Short-Term Price Patterns and Opening Range
Breakout" — NR7 / volatility contraction→expansion.** A narrow-range day (NR7 =
narrowest range of 7) tends to precede an expansion/trend day. Volatility
mean-reverts: quiet day → expansion next; wide day → contraction/rotation next.
Causal (known at the open).
[StockCharts](https://chartschool.stockcharts.com/table-of-contents/trading-strategies-and-models/trading-strategies/narrow-range-day-nr7)

## What held up on OUR data (2025 = 67 days, 2026 = 16 days)

| literature predictor | vs today's one-sidedness | verdict |
|---|---|---|
| **Crabel prior-day range %ile** | **rho −0.36 (p=0.00), 2025** | REAL — narrow prior day → trend today; wide prior → range (0.73 vs 0.85). Best, and causal at the open |
| Gao intraday momentum (first-30 return) | rho +0.00 (2025), +0.46 (2026) | weak/noisy in our sample; matches our VWAP-side result |
| Market-Profile IB width | rho +0.05, wide-IB 0.79 vs narrow 0.81 | NULL — the MP headline does not hold here |

## The decisive negative — classification is not the fade's bottleneck
Gating the fade on Crabel's signal (fade only wide-prior "rotational" days) made
capture **worse, not better**: wide-prior days cleared 20% on **0%** of days
(mean −8%) vs narrow-prior 9%. Why: wide-prior days have BIGGER ranges (median
110pt vs 72pt), so the "20% of range" bar is 22pt while the mechanical fade
captures a roughly *fixed* ~10pt (VWAP-to-2σ). The metric is confounded with
volatility — the fade clears the % bar more easily on quiet days regardless of
whether it's a "range" day.

## Honest conclusion
1. **The literature agrees reliable classification is a moderate-tilt problem,
   not a solved one.** Gao's momentum is a notable *anomaly* precisely because
   clean predictability would be arbitraged away. Our best causal predictor
   (Crabel, rho −0.36) explains ~13% of variance — real, but a lean, not a switch.
2. **Classification is not what's blocking the fade.** Even with the best
   literature signal, the mechanical fade misses your 20% bar — the gap is
   execution (fixed-point capture vs a range-scaled bar) and your discretion, not
   day-labelling.
3. **Realistic uses:**
   - **Ensemble the moderate tilts** (Crabel prior-range + Gao/VWAP-side + a
     volume/relative-volume gate — Gao says the effect is stronger on high-volume
     days) as a *co-pilot / sizing* input, not a hard on/off gate. Best-case this
     shifts the odds, doesn't guarantee.
   - **Reconsider the success metric**: "20% of range" penalises volatile days
     unfairly for a fixed-point strategy; a fixed-points-per-day target (e.g. ≥10
     ES pts) may be the honest bar for a mean-reversion sleeve.
   - The one causal, published, testable idea still un-mined: **Gao's volume
     conditioning** — fade only on LOW relative-volume mornings (rotational),
     stand aside on high-volume (institutional/trend). Next test.

## Follow-up tests (2026-07-15)

**Volume conditioning (Gao).** First-hour relative volume gating the fade:
- 2025 (real volume, 67d): NULL — low-vol AM −3% vs high-vol −4%, no difference.
- 2026 (thin feed, 16d): high-vol mornings got the fade **crushed (−17%, 0%
  positive, −8.5pt median)** while low-vol were slightly green (+3%). Matches Gao,
  but only on the delayed feed. Verdict: "**don't fade high-volume mornings**" is
  a defensible risk filter; the low-vol side is not a strong positive.

**Asia/Europe → US session (the user's memory; ICT "AMD" framing — already
rejected as unfalsifiable, but the quantitative core = Crabel).** 19 recorded
24h days: quiet Asia (rho −0.25) and quiet Europe (rho −0.23) both lean toward a
more *efficient/trending* US session — the RIGHT sign (quiet overnight →
expansion, matching Crabel and the user's recollection) — but **not significant
at n=19**, and the quiet-vs-active split is tiny (US efficiency 0.06 vs 0.04).
Pre-registered forward test: re-run once the recorder has ≥40 full 24h days
(~mid-Aug); this is the most promising thread because it matches both the user's
intuition and the one predictor that already showed signal.

## The consistent meta-truth (across every predictor tested)
VWAP-side (+0.45) · Crabel prior-range (−0.36) · Gao momentum (weak) · IB width
(null) · volume (mixed) · Asia/Europe overnight (right sign, n-starved). **Every
day-character signal is a moderate tilt (|rho| ≤ 0.45), never a clean switch** —
exactly what efficient-markets logic predicts (a reliable classifier would be
arbitraged). The honest engine use is an **ensemble of tilts as a sizing / co-pilot
input**, not a hard gate; and to keep recording 24h data so the session test
(the best-aligned idea) can be run with real power.
