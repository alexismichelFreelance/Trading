# Gamma exposure (GEX) — findings on the Feb–May 2025 ES window

**Source (free):** SqueezeMetrics daily DIX/GEX CSV (aggregate SPX dealer-gamma model,
EOD, 2011→present). Fetched + loaded to QuestDB `claude_gex` by `engine/tools/fetch_gex.py`;
served causally by `engine/engine/features/gamma.py` (`GammaRegime`). Trading day D uses the
PRIOR session's `gexp` = trailing-252-session percentile of GEX (no in-sample normalization).

## Strike-level (flip/walls) source verification — 2026-07-02
Historical per-strike chains with OI for Feb–May 2025 are **not freely available anywhere**:
- **OptionsDX**: VERIFIED — SPX EOD chains offered are **2010–2023 only** (no 2024/2025, no OI
  field). This is why it was dropped; `build_gex.py` stays as the consumer if that ever changes.
- **DoltHub `post-no-preference/options`**: greeks but **no OI and no volume** — unweightable.
- **CBOE free downloads**: aggregate volume / put-call ratios only; per-strike EOD = paid DataShop.
- **Wayback Machine** on the CBOE chain JSON: ~7 snapshots ever, nearest to our window is 2023.

**Forward-only alternative (LIVE, working):** CBOE's free delayed-quotes JSON
(`cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json`) carries the FULL chain with **true
open interest, volume, and greeks**. `engine/tools/fetch_cboe_gex.py` runs daily: computes the
dealer-gamma profile (true-OI-weighted, near-dated ≤7 DTE incl. SPXW), extracts zero-gamma flip /
call wall / put wall / net regime into QuestDB `claude_gex_levels`, and archives the raw JSON to
`gamma/raw_cboe/` — the RecorderTee philosophy: collect forward, never re-buy. First live row:
2026-07-02 spot 7483, flip 7545.7, walls 7500/7500, long-gamma (1,350 options).

## A. GEX predicts next-day RANGE — and adds beyond vol persistence
70 joined sessions (2025-02-19 → 05-30):
- rank(gexp_prev, next-day RTH range) = **−0.63**
- rank(prev-day range, next-day range) = +0.54 (the known vol-persistence signal)
- **partial rank(gexp | prev-range) = −0.43** — genuine incremental information
- tercile means: LOW-gex days range **120pt**, MID 66pt, HIGH 61pt

## B. GEX does NOT predict direction/trendiness
rank(gexp_prev, day efficiency) = −0.09. Size, not sign — consistent with every other
finding in this project (direction is not predictable from state variables).

## C. The sleeves align with the dealer-hedging mechanism ($/day by gexp tercile)
| bucket | ignition | open-drive | flow | zones | PORT |
|---|---|---|---|---|---|
| LOW (short-gamma) | **+462** | **+787** | +99 | +12 | +1,359 |
| MID | −66 | −114 | −28 | **+584** | +376 |
| HIGH (long-gamma) | +170 | +476 | −27 | **+311** | +930 |

Trend sleeves earn when dealers amplify (short gamma); the zones/structure sleeve earns when
dealers pin (long gamma). This is the textbook mechanism, visible in our own book.

## D. One-cut causal allocation rule (validated directionally)
**Trend sleeves (ignition, open-drive, flow) ON only when gexp_prev ≤ 1/3; zones always ON.**
| | total | maxDD | note |
|---|---|---|---|
| unconditional book | $82,830 | −$21,645 | |
| **GEX rule** | **$79,915 (96%)** | **−$15,744 (−27%)** | same t=2.1, all months + |
| inverse rule (sanity) | $10,131 | −$18,817 | mechanism confirmed (8× asymmetry) |

On this window the rule is a **drawdown reducer, not a P&L adder**; its expected value is
larger out-of-window: in a long calm (high-GEX) regime it keeps the trend sleeves from
bleeding through months of chop (their known failure mode).

### D.1 Wired live + replay-verified (2026-07-11)
The rule is now a live allocation gate — `engine/core/regime.py` `RegimeGate`, consulted by
`LiveEngine` before risk vetting, built by `run_live.py --gex-gate` from the causal
`GammaRegime`. It suppresses only trend-sleeve ENTRIES on known long/mid-gamma days; exits,
reduces, zones and IBS always pass; fail-open on unknown regime. `portfolio_report.py
--gex-gate` reproduces this table from fresh 65-day replay dumps: 43 short-gamma days (trend
ON) / 22 long-mid (trend OFF); **gated $79,806 (96% of ungated $82,720) at maxDD $-15,744
(-27%)** — matching the row above to the dollar on drawdown. 8 gate unit tests; replay/parity
paths are ungated (gate defaults OFF), so all 12 parity gates stay pinned.

## E. DIX bonus (noted, NOT wired)
High prev-day DIX tercile → next-day ES +17.4pt mean, 60% up (LOW/MID negative). The
documented dark-pool drift, present here — a possible future long-bias overlay, but 70 days
with lopsided buckets: needs more data before use.

## Honest limits
- 70 sessions; the LOW-GEX bucket heavily overlaps the April-crash regime (confounded with
  "April was wild") — the partial correlation (A) mitigates but doesn't eliminate this.
- SqueezeMetrics GEX is a naive-dealer-positioning MODEL (all calls dealer-long, puts
  dealer-short), not observed positioning.
- Buckets are lopsided (45/12/9) because trailing percentiles were depressed post-crash.
- EOD-only: no intraday gamma updates; 0DTE flows invisible.
