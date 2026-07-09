# Adaptive-flow study — modulating flow's threshold (2026-07)

**Ask (user):** flow's result swings wildly with a magic absolute `th` (200 → robust, 100 →
May negative, 30 → April-only). That fragility means it should be ADAPTIVE — modulate `th` with
market conditions. Find the correct modulation.

**Why it's the right instinct:** ignition trades on ANY feed because its signal is RELATIVE
(`str = |adelta| / rolling-mean`, scale-invariant); flow uses an ABSOLUTE `th=200` calibrated to
the research tape. Research per-sec `|adelta|`: mean 15, **std 32** (fat-tailed) — so `th=200` is
a ~5-6σ threshold. The live NT feed: mean ~12, **max ~66** (thin tail) — 5-6σ ≈ 84 > 66, so it
NEVER fires. A z-score threshold adapts to each feed's own shape.

## Laws tested (th_t causal from a trailing 30-min |adelta| window, per-day reset, turnover cost 0.30)
| law | k | net_pts | pooled all-mo+ | per-contract all-mo+ | median th |
|---|---|---|---|---|---|
| FIXED | 200 | +811 | ✓ | ✓ | 200 |
| ZSCORE `m+kσ` | 3 | −309 | ✗ | ✗ | 93 |
| | 3.5 | +401 | ✓ | ✓ | 105 |
| | **4** | **+939** | ✓ | ✓ | 118 |
| | 4.5 | +299 | ✗ | ✗ | 131 |
| | 5 | +257 | ✓ | ✓ | 144 |
| | 5.5 | +59 | ✗ | ✗ | 156 |
| | 6 | −14 | ✗ | ✗ | 169 |
| MEAN `k·mean` | 5/8/13 | −248/+236/+212 | ✗ | ✗ | 78/125/203 |

## The honest finding: adaptation does NOT stabilise flow
The z-score landscape is **spiky and non-monotonic** — +939 at k=4 sits between +401 (k=3.5) and
**+299/not-robust (k=4.5)**; k=3 is −309. Robust values (3.5, 4, 5) are interleaved with bad ones
(3, 4.5, 5.5). **k=4's peak is largely luck**, and with only 4 monthly points × 2 contracts the
robust/not-robust flips are within noise. So:
- Adaptive is **not more robust and not reliably more profitable** than the fixed threshold.
- Flow's parameter-fragility is **intrinsic** — a thin edge that any threshold change perturbs.
  This is a standing amber flag on flow as a real-money sleeve, independent of fixed vs adaptive.
- `MEAN·k` normalisation is strictly worse (never all-months-positive) — normalising by the LEVEL
  doesn't help; the SHAPE (std) is what differs across feeds/periods.

## What adaptation DOES buy (the real reason to use it)
**Scale-invariance.** `th_t = mean + 4σ` fires on whatever distribution the feed delivers — on the
live NT feed (mean 12, σ ~12) that's th ≈ 60, so flow finally responds to genuine ~4σ surges,
where the fixed `th=200` is dead. That solves the user's actual problem (flow alive on this feed)
without pretending the edge got better.

## Decision (shipped)
- **Live: adaptive on, `k=4`** (`FlowFollowingStrategy(adaptive=True, adapt_k=4)`). k=4 chosen on
  PRINCIPLE — "trade a 4-sigma genuine flow surge" — which also lands in the robust cluster; NOT
  because it maximised P&L (that would be overfitting the +939).
- **Default/parity: fixed `th=200`** unchanged (`adaptive=False` path is byte-identical; flow
  replay parity +874 preserved).
- **Flow stays the FRAGILE sleeve:** treat live flow P&L as observation, size it smallest, and
  re-test robustness once RecorderTee accumulates out-of-sample data.

Script: `strategy_lab/flow_adaptive.py` (reproducible). `run_replay --adaptive --adapt-k`,
`run_live` flow uses adaptive k=4.
