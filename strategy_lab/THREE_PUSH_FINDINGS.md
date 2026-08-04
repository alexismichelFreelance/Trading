# "Three pushes then chop/reversal" — hypothesis test (2026-07)

**Claim (user observation; also trader folklore — Raschke's Three Little Indians, three-drives,
Elliott impulse counts):** intraday moves happen in 3 pushes, then chop or reversal.

**Test (fully causal, mechanical):** zigzag on 1m closes (threshold θ), a "push" = a confirmed
swing high strictly above the previous one (mirror for lows; pooled). At each push-N
confirmation, race forward within the session: CONTINUE (new extreme beyond push-N pivot first)
vs REVERSE (close through the pullback pivot first) vs CHOP (neither by the close).
If the claim is true, P(continue) must DROP at N=3. **Null model:** identical machinery on
within-session shuffled 1m returns (same volatility, all temporal structure destroyed), 3 reps.
72 sessions, both contracts, θ ∈ {3, 5, 8}pt + vol-scaled (0.25× first-30m range).

## Result: the pattern is a property of randomness, not of the market

P(continue | push N), θ=5pt (other θ equivalent):

| N | real ES (n) | shuffled null (n) |
|---|---|---|
| 1 | 32% (1659) | 33% (4934) |
| 2 | 54% (769) | 53% (2334) |
| **3** | **54% (378)** | **50% (1110)** |
| 4 | 56% (174) | 53% (488) |
| 5+ | 47% (131) | 51% (430) |

- **No drop at N=3** in real data — continuation stays ~50–56% from push 2 onward, flat.
- **Real ≈ shuffled null at every N and every θ** (N=3 difference 4% ± 5.8% at 95% — noise).
  A random walk produces identical push statistics.
- **Push sizes don't shrink** (θ=3: push2 15.1pt, push3 15.2pt, push4 14.5pt) — no
  momentum-exhaustion signature into the "third push".
- θ=8 shows a mild late decline (N5+ 42%) — small n (73), inside noise, and present in nulls.

## Why the pattern *looks* real on charts
Push sequences die roughly geometrically (~half survive each round: 1659 → 769 → 378 → 174 …).
So completed 3-push structures occur several times per day at chart-scale θ — frequent enough
to be salient — and after ANY push the move "chops or reverses" about half the time by
construction. The eye keeps the beautiful completed three-drives-then-reversal cases and
discards the two-push and four-push cases. The zigzag geometry plus survivorship of memorable
examples IS the pattern. (Same epistemic failure mode as the ICT material we tested.)

## Trading implication
**None.** Fading the 3rd push is fading a ~52% coin flip, minus 0.517pt costs. Exiting trend
positions "because it's the third push" throws away a still-~54% continuation. If anything the
data says trends are **memoryless in push count** — consistent with this project's core
meta-finding: direction is not predictable from state; only event+capture structures
(ignition/trailing, open-drive, zones) and daily mean reversion (IBS) have survived.

Caveat: one mechanical definition was tested (close-based zigzag, strict HH/LL counting,
within-session). Adding discretionary context (trend filters, divergence, displacement) can
always rescue the pattern visually — but then it is no longer falsifiable, which is the point.
