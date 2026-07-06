# Overnight (ETH) session — data unlock + flow test (2026-07)

**Discovery:** the raw `mbo_events` tape contains the FULL ~23h Globex session — the 13–21 UTC
filter was only applied when building the research feature tables. Built `claude_sec_eth`
(per-second pxc/adelta/avol for all hours OUTSIDE 13–21 UTC, both contracts, 1.74M rows;
rebuildable via `engine/tools/build_eth_features.py`). Flow needs no book columns, so it is
fully testable overnight; ignition would additionally need overnight bid/ask add/cancel
reconstruction (not built).

## Overnight liquidity, quantified on our own tape
- RTH averages **45 contracts/sec**; overnight ≈ **4/sec on the full grid (ratio 0.08 — ~12×
  thinner)**, ~10/sec counting only seconds that trade at all.
- The flow threshold `|adelta|≥200` fires **0.36%** of RTH seconds but only **0.055%** of
  *traded* overnight seconds — effectively never on the grid.

## Flow overnight (sessions = 21:00→13:00 UTC, strict 1s grid, 72 nights)
| config | net @0.30 | net @0.60 | months (Feb/Mar/Apr/May) | notes |
|---|---|---|---|---|
| standard TH=200/SCALE=3000 | +76 | +76 | 0 / 0 / 0 / +76 | **2 position changes in 72 nights** — dormant; the +76 is one May night |
| vol-scaled TH=20/SCALE=240, maxp=50 | +2,327 | +1,817 | −362/−223/+1,121/+1,791 | **fantasy sizing**: ±50 lots in a 4-lot/sec book |
| same, **maxp=5 (realistic)** | +598 | **+166** | **−515/−310/−46/+1,038** | 46% winning nights; **top-5 nights = 719% of total** |
| same, maxp=2 | | **−64** | +77/−55/−299/+214 | negative outright |

**Verdict: no overnight flow edge.** The standard strategy has nothing to do (validating the
live `gate_utc=(13,21)`); the rescaled variant's headline evaporates at realistic size and
honest costs — three of four months negative, the entire total from ~5 May news-gap nights
(the other 67 nights net −1,030pt). Textbook mirage via oversizing into outlier nights, and
the TH/SCALE rescale was itself a calibration (multiplicity). Gate stays.

## What the ETH data IS good for (next uses)
1. **Context features for RTH sleeves** — overnight range/gap/drift as inputs to the day book
   (e.g., condition open-drive on the overnight range; Asian/London-window ranges are now
   computable). The most promising use: overnight as *context*, not as a trading session.
2. **True 24h day-range for IBS** — the live NT feed already provides it; the ES=F validation
   (t=4.2) already covers this variant.
3. Event-window behavior (the 2–3am news bursts where the rescaled flow made its May money)
   — needs an economic-calendar feed; parked.
4. Overnight ignition — requires rebuilding book add/cancel columns for ETH hours from raw
   MBO (bigger job); ignition's edge was validated WITH book-confirm, so no shortcut. Parked.
