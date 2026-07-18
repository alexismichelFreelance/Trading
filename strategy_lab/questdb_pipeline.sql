-- ============================================================================
-- ES research pipeline — reproducible QuestDB SQL (run via the Chrome bridge:
--   fetch('/exec?query=' + encodeURIComponent(SQL)) on http://localhost:9000)
-- Re-run this end-to-end whenever new data is loaded into mbo_events.
-- Conventions (VERIFIED): trades = is_trade=true ; delta(buy+) = sum(B) - sum(A).
-- Keep each statement < 45s (browser eval cap). INSERTs complete server-side
-- even if the client times out — verify per-day counts, never blind-retry.
-- ============================================================================

-- STEP 0. Discover coverage (cheap; avoid count_distinct over full table)
-- SELECT min(ts_recv), max(ts_recv) FROM mbo_events;
-- SELECT DISTINCT symbol FROM mbo_events;

-- STEP 1. 1-minute order-flow bars (hours 13-21 UTC superset; RTH cut later).
-- Run the INSERT once per ~5 trading days per symbol to stay under 45s.
CREATE TABLE IF NOT EXISTS claude_bars_1m (
  symbol SYMBOL, ts TIMESTAMP, o DOUBLE, h DOUBLE, l DOUBLE, c DOUBLE,
  vol LONG, delta LONG, ntr LONG, vwap DOUBLE) TIMESTAMP(ts) PARTITION BY DAY;

-- template (substitute SYM, START, END = exclusive, ~weekly chunks):
-- INSERT INTO claude_bars_1m
-- SELECT symbol, ts_recv ts, first(price) o, max(price) h, min(price) l, last(price) c,
--   sum(size) vol,
--   sum(CASE WHEN side='B' THEN size ELSE 0 END) - sum(CASE WHEN side='A' THEN size ELSE 0 END) delta,
--   count() ntr, sum(price*size)/sum(size) vwap
-- FROM mbo_events
-- WHERE symbol='SYM' AND is_trade=true
--   AND ts_recv>='STARTT00:00:00.000000Z' AND ts_recv<'ENDT00:00:00.000000Z'
--   AND hour(ts_recv)>=13 AND hour(ts_recv)<21
-- SAMPLE BY 1m ALIGN TO CALENDAR;
-- verify: SELECT to_str(ts,'yyyy-MM-dd') d, count() n FROM claude_bars_1m GROUP BY d ORDER BY d; (expect 480/day)

-- STEP 2. RTH (ET 09:30-16:00, DST-correct) + session VWAP & sigma bands.
DROP TABLE IF EXISTS claude_rth_1m;
CREATE TABLE claude_rth_1m AS (
  SELECT symbol, ts, et, sess_date, et_min, (et_min-570) mos, o,h,l,c,vol,delta,ntr, bar_vwap,
    (cum_pv/cum_v) sess_vwap,
    sqrt(CASE WHEN (cum_pv2/cum_v - (cum_pv/cum_v)*(cum_pv/cum_v))>0 THEN (cum_pv2/cum_v - (cum_pv/cum_v)*(cum_pv/cum_v)) ELSE 0 END) sess_sigma
  FROM (
    SELECT symbol, ts, et, sess_date, et_min, o,h,l,c,vol,delta,ntr, bar_vwap,
      sum(vol) OVER w cum_v, sum(vol*bar_vwap) OVER w cum_pv, sum(vol*bar_vwap*bar_vwap) OVER w cum_pv2
    FROM (
      SELECT symbol, ts, to_timezone(ts,'America/New_York') et,
        timestamp_floor('d', to_timezone(ts,'America/New_York')) sess_date,
        hour(to_timezone(ts,'America/New_York'))*60 + minute(to_timezone(ts,'America/New_York')) et_min,
        o,h,l,c,vol,delta,ntr, vwap bar_vwap
      FROM claude_bars_1m
    ) WHERE et_min>=570 AND et_min<960
    WINDOW w AS (PARTITION BY symbol, sess_date ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
  )
);

-- STEP 3. Regime feature: z-score vs VWAP + Kaufman efficiency ratio (look-ahead-free).
DROP TABLE IF EXISTS claude_rth_feat;
CREATE TABLE claude_rth_feat AS (
  SELECT symbol, ts, sess_date, mos, c, delta, vol, sess_vwap, sess_sigma,
    (c-sess_vwap)/sess_sigma z,
    abs(c-fv)/(CASE WHEN cumabs>0 THEN cumabs ELSE 1000000000 END) er
  FROM (
    SELECT symbol, ts, sess_date, mos, c, delta, vol, sess_vwap, sess_sigma,
      first_value(c) OVER w fv, sum(abs(ret)) OVER w cumabs
    FROM (
      SELECT symbol, ts, sess_date, mos, c, delta, vol, sess_vwap, sess_sigma,
        c - lag(c) OVER (PARTITION BY symbol, sess_date ORDER BY ts) ret
      FROM claude_rth_1m
    ) WINDOW w AS (PARTITION BY symbol, sess_date ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
  )
);

-- STEP 4. Level set: per-session RTH H/L/C -> prior-day floor pivots + PDH/PDL + round numbers.
DROP TABLE IF EXISTS claude_levels_daily;
CREATE TABLE claude_levels_daily AS (
  SELECT d.symbol, d.sess_date, d.h rth_h, d.l rth_l, cl.c rth_c,
    lag(d.h) OVER w ph, lag(d.l) OVER w pl, lag(cl.c) OVER w pc
  FROM (SELECT symbol, sess_date, max(h) h, min(l) l FROM claude_rth_1m GROUP BY symbol, sess_date) d
  JOIN (SELECT symbol, sess_date, c FROM claude_rth_1m WHERE mos=389) cl
    ON d.symbol=cl.symbol AND d.sess_date=cl.sess_date
  WINDOW w AS (PARTITION BY d.symbol ORDER BY d.sess_date)
);
-- claude_levels_long: UNION ALL of PP,R1,S1,R2,S2,R3,S3,PDH,PDL (PP=(ph+pl+pc)/3) + 'RND' (round/25)
-- claude_levels_conf: self-join count of levels within 1.5pt (confluence).

-- ============================================================================
-- KEY SCREENS (full-sample, compact output). See ES_Research_Findings.md for round-1 results.
--  A. Order-flow continuation by horizon & time-of-day (open/mid/close).
--  B. VWAP extension episode backtest: 2.5sigma crossing, momentum vs fade, by efficiency-ratio regime.
--  C. Level-touch fade pooled + by confluence (isolated vs pivot-coincides-round-number).
--  D. Parameter-free MFE/MAE over the forward window (decides if bracket exits can help).
-- Round-1 verdict: faint structure (open order flow; confluent reversals; 2.5sigma inflection),
-- none robust net of 0.52pt cost across both contracts on ~21 days. Re-run on more data to validate.
-- ============================================================================
