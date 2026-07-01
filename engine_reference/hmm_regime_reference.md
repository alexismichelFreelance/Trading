# Reference: 1-hour regime HMM + ignition exit routing (the +459 → +1004 fix)

This is the canonical, reconstructed-from-source logic for the piece that closes the Gate-B parity gap.
The engine reproduced **+459** = ignition entries + BOOK chop-exit applied everywhere. The missing **+545**
is the **1h-HMM regime gate** routing *trend* hours to the zone/pivot-target ride instead of BOOK.

Two facts that make this fully recoverable:
1. The HMM is **deterministic** (fixed percentile init + fixed 30 iterations on fixed 1h-efficiency data).
   `hmm_1h_params.json` are the regenerated frozen params; `hmm_1h_states.csv` is the resulting per-hour
   classification. The engine should **load** the params (and/or the states), never refit.
2. The exit-routing code below is the exact logic that produced +913 (pivot target) / **+1004** (zone target).

---

## A. Efficiency-ratio feature (per 1h bar, per day, causal)
For each session day, over its 1h bars `c[0..]`, with lookback `L=3`:
```
er[m] = (m < L) ? null
                : | c[m] - c[m-L] |  /  sum_{j=m-L+1..m} | c[j] - c[j-1] |    (0 if denom==0)
```
The HMM filter **resets each day** (new forward pass per day). For `m < L`, `er=null` -> emit state 0 (chop).

## B. Frozen-HMM causal forward-filter (load params, do NOT refit)
For each day, walk bars left-to-right maintaining the filtered posterior `f` (normalized):
```
B = [ N(er | mu[0], var[0]) , N(er | mu[1], var[1]) ]   // N = Gaussian pdf (+1e-12)
f = (first bar) ? [ pi[0]*B[0], pi[1]*B[1] ]
               : [ B[0]*(f0*A[0][0] + f1*A[1][0]),  B[1]*(f0*A[0][1] + f1*A[1][1]) ]
f = f / sum(f)
state_raw = (f[0] > f[1]) ? 0 : 1
regime[hour] = (state_raw == trend_state_index) ? TREND(1) : CHOP(0)
```
`trend_state_index = 1` (from params). Key the result by the hour string `"yyyy-MM-dd HH:00"`.

**Regenerate the full per-hour states yourself** (deterministic) by running fbFit on the ESM5 1h-er
series and the forward-filter above on both contracts. Verify your fitted params equal
`hmm_1h_params.json` — in particular `mu` must come out `[0.4427, 0.9997]` (chop ER 0.44 / trend ER 1.0).
Spot-check the first ESM5 hours against this sample (1 = TREND, 0 = CHOP):
```
2025-04-01 13:00,0   2025-04-03 18:00,1   2025-04-04 20:00,1   2025-04-08 17:00,1
2025-04-01 14:00,0   2025-04-03 19:00,1   2025-04-07 13:00,0   2025-04-08 18:00,1
2025-04-01 17:00,0   2025-04-03 20:00,1   2025-04-07 20:00,0   2025-04-08 19:00,1
2025-04-01 18:00,1   2025-04-04 13:00,0   2025-04-08 16:00,0   2025-04-08 20:00,1
```
(Trend hours are sparse — most RTH hours are chop; the +545 comes from the few trend hours where the
trade rides to the zone target instead of taking the BOOK exit.) If your params match and these hours
match, your state series is correct; proceed to the routing.

### Exact fbFit (for completeness / re-derivation; produces hmm_1h_params.json deterministically)
```js
function fbFit(X){ // X = array of er values across ALL ESM5 days (concatenated, nulls excluded)
  const N=X.length, ss=[...X].sort((a,b)=>a-b);
  let mu=[ss[Math.floor(N*0.2)], ss[Math.floor(N*0.8)]], vr=[0.03,0.03], pi=[0.5,0.5],
      A=[[0.92,0.08],[0.08,0.92]];
  const g=(x,m,v)=>Math.exp(-(x-m)*(x-m)/(2*v))/Math.sqrt(2*Math.PI*v)+1e-12;
  for(let it=0; it<30; it++){            // 30 iterations
    const B=X.map(x=>[g(x,mu[0],vr[0]), g(x,mu[1],vr[1])]);
    // forward (scaled)
    const al=X.map(()=>[0,0]), be=X.map(()=>[0,0]), c=Array(N).fill(0);
    al[0]=[pi[0]*B[0][0], pi[1]*B[0][1]]; c[0]=al[0][0]+al[0][1]; al[0]=[al[0][0]/c[0], al[0][1]/c[0]];
    for(let q=1;q<N;q++){ for(let k=0;k<2;k++) al[q][k]=B[q][k]*(al[q-1][0]*A[0][k]+al[q-1][1]*A[1][k]);
      c[q]=al[q][0]+al[q][1]; al[q][0]/=c[q]; al[q][1]/=c[q]; }
    be[N-1]=[1,1];
    for(let q=N-2;q>=0;q--) for(let k=0;k<2;k++)
      be[q][k]=(A[k][0]*B[q+1][0]*be[q+1][0]+A[k][1]*B[q+1][1]*be[q+1][1])/c[q+1];
    const ga=X.map(()=>[0,0]), xs=[[0,0],[0,0]];
    for(let q=0;q<N;q++){ const z=al[q][0]*be[q][0]+al[q][1]*be[q][1];
      ga[q][0]=al[q][0]*be[q][0]/z; ga[q][1]=al[q][1]*be[q][1]/z; }
    for(let q=0;q<N-1;q++) for(let i=0;i<2;i++) for(let k=0;k<2;k++)
      xs[i][k]+=al[q][i]*A[i][k]*B[q+1][k]*be[q+1][k]/c[q+1];
    pi=[ga[0][0],ga[0][1]];
    for(let i=0;i<2;i++){ const tt=xs[i][0]+xs[i][1]; A[i][0]=xs[i][0]/tt; A[i][1]=xs[i][1]/tt; }
    for(let k=0;k<2;k++){ let sg=0,sx=0; for(let q=0;q<N;q++){sg+=ga[q][k];sx+=ga[q][k]*X[q];}
      mu[k]=sx/sg; let sv=0; for(let q=0;q<N;q++) sv+=ga[q][k]*(X[q]-mu[k])**2; vr[k]=Math.max(1e-4,sv/sg); }
  }
  return {mu, vr, pi, A, tS: mu[0]>mu[1]?0:1};   // tS = TREND state index = higher-efficiency mean
}
```

## C. Ignition exit ROUTING (the +545). This is the part to port exactly.
Per validated entry (`i0`, dir `s`, entry px `px0`, entry-second epoch `te`; `end = min(n-1, i0+600)`):
```
t60 = regime[ hour_of(i0) ] == TREND        // from the frozen HMM above

// --- targets ---
// trend target = nearest opposing VIRGIN 30m S/D zone beyond entry (>= MINT=2 pt away), else nearest floor pivot
opp  = zones where dir == -s AND te in [formMs, invalMs] AND (s>0 ? bot > px0+2 : top < px0-2)
zTgt = opp ? (s>0 ? min(opp.bot) : max(opp.top)) : null
pTgt = nearest pivot beyond entry (>=2pt)          // PP,R1-3,S1-3 from prior session H/L/C + PDH/PDL + 50-round
tgt  = zTgt != null ? zTgt : pTgt                  // ZONE target gives +1004; pivot-only gives +913
lstop = nearest opposite-side level                // pivot stop

// --- single forward walk, record first hit of each event ---
for k in i0+1..end:
  FE = s*(px[k]-px0); peakFE = max(peakFE, FE)
  if bookIdx<0 and peakFE>=2 and (k-i0)>=8:
      netLead = s>0 ? netAsk[k] : netBid[k]          // rolling-10s (add-cancel) on LEADING side
      if netLead>200 and FE<=0.8*peakFE: bookIdx=k    // BOOK-healing chop exit
  if fa4<0  and FE<=-4:  fa4=k                        // chop hard stop
  if faCap<0 and FE<=-12: faCap=k                     // trend max-loss cap
  if znIdx<0:
      if tgt!=null   and (s>0?px[k]>=tgt :px[k]<=tgt ): znIdx=k; znPx=tgt
      elif lstop!=null and (s>0?px[k]<=lstop:px[k]>=lstop): znIdx=k; znPx=lstop

hz = s*(px[end]-px0) - COST                           // horizon fallback
if t60:   // TREND -> ride to target / level-stop, capped at -12
    pnl = (znIdx>=0 and (faCap<0 or znIdx<=faCap)) ? s*(znPx-px0)-COST
        : (faCap>=0 ? -12-COST : hz)
else:     // CHOP -> BOOK exit, else -4 stop, else horizon
    pnl = (bookIdx>=0 and (fa4<0 or bookIdx<=fa4)) ? s*(px[bookIdx]-px0)-COST
        : (fa4>=0 ? -4-COST : hz)
```
`COST = 0.517` (research). `netAsk/netBid[k]` = rolling 10-second sum of `(ask_add-ask_cancel)` /
`(bid_add-bid_cancel)`. Entry filter (already matching in your engine): `str>=5 & avol>=800 &
book-confirm (long: ask_cancel>bid_cancel) & trend-aligned (sign(px[i]-px[i-300])==s)`.

## D. Parity expectation
With the frozen states loaded and the routing above, the four month-buckets should reproduce:
`ESM5_Apr +622 / ESM5_May +83 / ESH5_Feb +42 / ESH5_Mar +166 = +913` (pivot target), and
`+699 / +72 / +64 / +170 = +1004` (zone target). If your per-hour states match `hmm_1h_states.csv`
and the routing matches above, parity closes. If states match but P&L still diverges, the bug is in the
zone set or the BOOK/level walk ordering, not the HMM.
```
