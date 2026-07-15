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
