# Bubble detection — BSADF + LPPLS, validated on the known bubbles (2026-07)

**Ask:** can we detect bubbles, and detect when one is about to pop — dotcom vs the current
AI era? The "earthquake model" = Sornette's **LPPLS** (log-periodic power law singularity:
bubbles as critical phenomena ending at a finite-time singularity tc, imported from rupture
physics). The econometric standard = **Phillips-Shi-Yu BSADF** (recursive right-tailed ADF:
dates periods where price is statistically EXPLOSIVE rather than random-walking).

**Implementation:** `engine/tools/bubble_scan.py` — vectorized BSADF (lag-0 ADF over all
window starts via prefix sums; 95% critical values from 99 Monte-Carlo random walks per
series length) on monthly log prices; LPPLS via Filimonov-Sornette calibration (linear
params solved by OLS; grid + Nelder-Mead over tc/m/ω). Data: Yahoo (^GSPC 1927+, ^IXIC
1971+, ^N225, BTC, NVDA), FRED (WTI 1946+, Case-Shiller 1987+).

## Validation 1 — BSADF dating vs the historical record (out-of-the-box hits)
| episode | BSADF explosive period | actual peak |
|---|---|---|
| **1987 crash** (S&P) | 1986-10 → **1987-09** | 1987-08-25 (crash Oct 19) |
| **Japan bubble** (Nikkei) | 1984-09 → **1990-07** | 1989-12-29 |
| **Dotcom** (NASDAQ) | 1995-05 → 1998-07, 1998-09 → **2001-01** | 2000-03-10 |
| **Dotcom** (S&P) | 1995-06 → 2001-08 | 2000-03-24 |
| **Oil, OPEC era** | 1970-12 → **1984-11** | 1980 spike (collapse 1986) |
| **Oil 2008** | 2008-04 → **2008-08** | 2008-07-03 |
| **Bitcoin 2017** | 2017-04 → **2018-04** | 2017-12-17 |
| **Bitcoin 2021** | 2021-01 → **2021-04** | 2021-04-14 (local top) |
| Post-COVID everything | 2020-11/12 → 2022-03/05 | 2021-11/2022-01 |

Every major labeled bubble is caught, usually years before the peak — and the **flag
turn-OFF lands at/just after the peak** (Nikkei 1990-07, oil 2008-08, BTC 2018-04, NASDAQ
2001). Caveat: on the smooth, autocorrelated Case-Shiller index the naive spec over-flags
(PSY used price/rent); treat housing separately.

## Validation 2 — LPPLS tc as a "pop timer": systematically LATE, do not trade it
Fits ending BEFORE each peak (true forecast test):
| bubble | tc estimate | actual peak | error |
|---|---|---|---|
| NASDAQ dotcom | 2000-08-10 | 2000-03-10 | **+5 months** |
| Nikkei 1990 | 1990-06-23 | 1989-12-29 | +6 months |
| Bitcoin 2017 | 2019-01-11 | 2017-12-17 | +13 months |
| S&P 2007 | 2008-06-24 | 2007-10-09 | +9 months |
| Oil 2008 | 2010-02-11 | 2008-07-03 | +19 months |
| Housing 2006 | 2008-06-29 | 2006-07-31 | +23 months |

6/6 late (median ≈ +11 months), fits often pin at parameter bounds (the documented LPPLS
calibration pathology). Sornette's group runs ensembles of thousands of fits with confidence
indicators; even that public record is mixed. **Single-fit tc is not a timing signal.**

## The AI-era answer (data through 2026-07-02)
- **BSADF says the AI era IS a statistically explosive regime, right now**: S&P flagged
  since **2023-12**, NASDAQ since **2024-05**, NVDA since **2023-01**, Nikkei joined
  **2026-01** — all still ON at the last close (NDX 29,329 / SPX 7,483). Same statistical
  class as dotcom/Japan/BTC — note dotcom stayed flagged ~5 years before popping.
- **LPPLS tc scatter** (window-dependent): 2026-08 → 2027-11 across NDX/SPX/NVDA windows;
  the most recent windows (2025-04→now) all give the NEAREST tc ≈ **2026-08/09** (steep
  recent acceleration), but with the +5..+23-month historical late bias, treat as context.

## Usage doctrine (what this is actually good for)
1. **Flag ON = bubble regime**, not imminent pop — a *posture* signal: it can run for years
   (1995→2000). Crashes come FROM explosive regimes, so: tighter risk on swing/overnight
   sleeves (IBS), hedges cheapened by call-heavy skew, no "short the bubble" hero trades.
2. **Flag turn-OFF after a long ON stretch = pop-in-progress confirmation** — on the record
   above it fired within ~1–4 months of every major peak with no false negative among the 7
   equity/commodity episodes. That is a *reduce/de-risk trigger*, confirmed monthly.
3. **LPPLS tc**: context only.
4. Cadence: run `tools/bubble_scan.py` monthly (it needs one new monthly close to update).

## Honest limits
Monthly resolution (confirmation lags weeks); 99-rep MC critical values (screen-grade);
lag-0 ADF spec (standard in PSY empirics but simplest); the turn-off rule is validated on 7
episodes — small sample of big events by nature; the AI-era call is a REGIME statement, not
a prediction of the pop date.
