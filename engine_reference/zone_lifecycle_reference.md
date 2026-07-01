# Reference: zone lifecycle (Gate C) — exact spec for fade / break / FLIP

Look-ahead-corrected version. Sized 2%/$2k, NT-ish cost. Parity targets ($, sized, on ESM5+ESH5):
**FADE +17,534 · BREAK +5,180 · FLIP +24,789 · COMBINED +47,502.** (FLIP is ~52% — get it exact.)
Operates on **30-minute bars** `B[]` from `claude_bars_1m` (`SAMPLE BY 30m ALIGN TO CALENDAR`, per symbol),
fields `o,h,l,c,v`. `n = B.length`.

## Constants
`PV=50` ($/pt), `RISK$=2000`, `MAXC=30`, `COST=0.5` ($/contract round-turn), `SCALP=4` (pt), `K=16` (max bars held).
`sizeFor(risk_pts) = max(1, min(MAXC, floor(RISK$ / (max(1,risk_pts) * PV))))`.

## 1. Zone detection (with composite strength)
`av(i,arr,w)` = mean of `arr` over the `w` bars before `i`. For each `j` in `4 .. n-6`:
```
aR = av(j, range, 20);  aV = av(j, vol, 20)            // range[k]=h-l, vol[k]=v
DEPARTURE: range[j] >= 1.4*aR  AND  |c[j]-o[j]| >= 0.5*range[j]  AND  v[j] >= aV
dir = c[j] > o[j] ? +1 : -1                            // +1 demand (up departure), -1 supply
base = the run of bars immediately before j (j-1 down to j-3) WHILE range[bar] <= 0.8*aR   // 1..3 bars
if base is empty: skip
top = max(base.h);  bot = min(base.l);  if top-bot < 0.5: skip
// composite strength (for fade quality bucketing; NOT P&L-critical):
disp = dir>0 ? (max(B[j..j+3].h) - top) : (bot - min(B[j..j+3].l))
depScore = disp/aR>=3 ? 2 : disp/aR>=1.5 ? 1 : 0;  baseScore = base.len<=2 ? 2 : base.len<=4 ? 1 : 0
zone = { j, dir, top, bot, compStr: depScore+baseScore }
```
Keep all zones in a list `Z` (you need it for look-ahead-safe target lookup).

## 2. Shared scale-out walker (half off at +4, breakeven runner to target)
```
walk(dir, entry, stop, target, si):
  risk = |entry-stop|;  size = sizeFor(risk);  sc = entry + dir*SCALP
  scH=stH=tgH = -1
  for k in si .. min(si+K, n)-1:
    if dir>0: if stH<0 && B[k].l<=stop: stH=k;  if scH<0 && B[k].h>=sc: scH=k;  if tgH<0 && B[k].h>=target: tgH=k
    else:     if stH<0 && B[k].h>=stop: stH=k;  if scH<0 && B[k].l<=sc: scH=k;  if tgH<0 && B[k].l<=target: tgH=k
    if stH>=0 && (scH<0 || stH<scH): break       // stop reached before the +4 scalp
    if tgH>=0: break
  if   stH>=0 && (scH<0 || stH<scH):  pA=-risk;  pB=-risk                              // full stop, both halves
  elif scH>=0:                        pA=SCALP;  pB = (tgH>=0 && tgH>=scH) ? |target-entry| : 0   // half +4; runner: target if hit AFTER scalp, else breakeven(0)
  else:  last=B[min(si+K-1,n-1)].c;   pA=dir*(last-entry);  pB=pA                       // timeout: mark to last close
  pnl_$ = ((pA+pB)/2) * PV * size  -  COST*size
  return pnl_$
```
Note: `target` for a long is above entry, for a short below. The walker checks scalp/stop/target by bar
high/low; "runner = target only if `tgH >= scH`" means the runner books the target only if it's reached
*after* the scalp (otherwise breakeven). KEEP this scale-out — it is the strategy, not a simplification.

## 3. The three setups (one pass per zone)
`prox = dir>0 ? top : bot` (the edge price first re-touches).
```
i = j+4;  fadeDone=false;  breakBar=-1
for i = j+4 .. n-1:
  touch = dir>0 ? (B[i].l<=top && B[i].l>=bot-0.5) : (B[i].h>=bot && B[i].h<=top+0.5)
  if touch && !fadeDone:
     # ---- FADE (fresh-zone reaction; 1st touch only) ----
     opp = Z where o.dir==-dir AND o.j < i AND (dir>0 ? o.bot>prox+3 : o.top<prox-3)   # opposing zone, formed BEFORE i
     tgt = opp ? (dir>0 ? min(opp.bot) : max(opp.top)) : prox + dir*|prox-(dir>0?bot-1:top+1)|*2   # else 2R
     fadeStop = dir>0 ? bot-1 : top+1
     emit FADE  walk(dir, prox, fadeStop, tgt, i)
     fadeDone = true
  if dir>0 ? B[i].c < bot-1 : B[i].c > top+1:  breakBar = i;  break    # zone BROKE (close through distal +1pt)
if breakBar < 0 or breakBar+2 >= n:  continue   # never broke (in-sample) -> no break/flip trade

# ---- BREAK (trade WITH the break) ----
bdir = -dir                                   # broken demand -> short; broken supply -> long
bEntry = B[breakBar].c
bStop  = dir>0 ? top+1 : bot-1                 # failed-break stop = back through the zone
oppB = Z where o.dir==dir AND o.j < breakBar AND (bdir>0 ? o.bot>bEntry+3 : o.top<bEntry-3)
bTgt = oppB ? (bdir>0 ? min(oppB.bot) : max(oppB.top)) : bEntry + bdir*|bEntry-bStop|*2
emit BREAK  walk(bdir, bEntry, bStop, bTgt, breakBar+1)
```

## 4. FLIP — fully specified (this is the part that was ambiguous)
A broken zone **flips polarity**: broken **demand → supply** (resistance), broken **supply → demand**
(support). After the break, find the **first retest from the broken side** and trade the flipped direction.
```
for q = breakBar+2 .. n-3:
  # retest from the broken side:
  #   broken DEMAND (dir>0): price is now BELOW; flip is touched when a bar HIGH rises back into the zone
  #   broken SUPPLY (dir<0): price is now ABOVE; flip is touched when a bar LOW drops back into the zone
  flipTouch = dir>0 ? (B[q].h>=bot && B[q].h<=top+0.5) : (B[q].l<=top && B[q].l>=bot-0.5)
  if flipTouch:
     fdir   = -dir                              # flipped direction (demand->supply => SHORT; supply->demand => LONG)
     fEntry = dir>0 ? bot : top                 # proximal edge of the FLIPPED zone (the level price re-touches)
     fStop  = dir>0 ? top+1 : bot-1             # stop just beyond the far edge of the old zone
     oppF   = Z where o.dir==dir AND o.j < q AND (fdir>0 ? o.bot>fEntry+3 : o.top<fEntry-3)   # next same-original-polarity zone in fdir
     fTgt   = oppF ? (fdir>0 ? min(oppF.bot) : max(oppF.top)) : fEntry + fdir*|fEntry-fStop|*2
     emit FLIP  walk(fdir, fEntry, fStop, fTgt, q)
     break                                      # ONE flip trade per broken zone
  # abort: if price runs away past the zone in the continuation direction by 5pt before any retest, no flip
  if dir>0 ? B[q].c > top+5 : B[q].c < bot-5:  break
```
Key points that disambiguate the flip:
- It only exists **after** the zone has broken (`breakBar` found).
- Entry direction is **opposite to the zone's original direction** (`fdir = -dir`).
- Entry price is the **near edge** of the old zone (`bot` for a broken-demand short, `top` for a broken-supply long).
- Stop is **just beyond the far edge** (old `top+1` / `bot-1`).
- Retest must come from the **broken side** (price returning to the zone from where it broke through).
- **One flip per zone**; abort if price never returns (ran 5pt past the zone first).

## 5. Parity / debugging
Reproduce per-setup: **FADE ≈ +$17,534 (n≈27), BREAK ≈ +$5,180 (n≈31), FLIP ≈ +$24,789 (n≈19)**.
If FADE matches but FLIP is off, the bug is in the retest-side condition or `fEntry/fStop`. The single
biggest correctness trap (already fixed here) is **look-ahead in targets**: every `opp/oppB/oppF` filter
includes `o.j < currentBar` — only zones that formed *before* the trade may be used as targets.
Per-month (combined): Feb +15.9k / Mar +16.6k / Apr +5.9k / May +9.2k (zones earn most in calm months).
