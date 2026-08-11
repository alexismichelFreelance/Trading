"""PivotStrategy — a mechanical copy of the user's discretionary pivot trading
(decoded from the 2026-07-23 session, +$4,375):

  1. Grid: classic floor pivots (PP, R1-R3, S1-S3) off the prior RTH session.
  2. A DIRECTIONAL BIAS for the day, computed at the US open from the overnight
     (Asia+London) action — the fuzzy part the user reads by eye:
        - VWAP posture: how much of the overnight price sat BELOW session VWAP
          (below-VWAP persistence was the strongest intraday-momentum signal in
          the earlier research, ~+0.45), and whether VWAP acted as resistance.
        - Character: Kaufman efficiency of the overnight move (trend vs "wiggle").
        - Short gamma amplifies conviction.
     -> bias in {-1 short, 0 neutral, +1 long}.
  3. Execution = resting limit-style entries at pivots, in the bias direction:
        short bias -> fade retraces UP into the nearest pivot above; cover at the
        next pivot down. Plus a DEEP-pivot reversal (buy S3 / sell R3 as "far
        enough for a bounce"), target back toward VWAP. Flat by 15:59 ET.
  (long bias mirrors.) Single position at a time; set-and-forget within the day.

NOT a validated edge — the bias read especially is a first cut to be tuned by
comparing its daily call to the user's actual trades (paper only). The execution
(pivots + limit touches + EOD flat) is the crisp, low-risk part.
"""
from __future__ import annotations

from ..core.events import Bar
from ..core.orders import Order, OrderType
from ..core.timeutil import et_minute_of_day, et_session_date
from ..features.pivots import MultiPivots
from .base import BaseStrategy

# ── session windows (ET minutes) ─────────────────────────────────────────────
RTH_START = 9 * 60 + 30      # 09:30 — US open; bias is frozen here
RTH_END = 16 * 60           # 16:00
EOD_FLAT = 15 * 60 + 59     # 15:59 — flat everything

# ── bias thresholds (TUNABLE — the research/fuzzy knobs) ─────────────────────
# Kaufman ER is NOT comparable across bar counts: for a random walk of N bars
# ER ~ RW_C/sqrt(N) (~0.05 at the N~570 one-minute bars of an overnight). The
# original absolute ER_MIN=0.30 was therefore unreachable and the sleeve never
# took a trade in 34 live sessions. Normalize by the random-walk baseline
# instead, so the knob means "how many times more directional than noise" and
# is invariant to bar count AND instrument.
RW_C = 1.25                 # E|net| / E(sum|steps|) * sqrt(N) for a random walk
ER_MIN_NORM = 1.10          # 1.0 == random walk. Calibrated on 28 live ES
                            # sessions (tools/pivot_bias_calib.py): fires on
                            # 6/28 (~21%) — the clearly-directional nights.
                            # Sanity: 2026-07-23, the user's +$4,375 pivot day,
                            # scores 2.28 (the sample MAX) and reads short;
                            # 2026-07-24's bear-trap chop scores 0.43 -> stand
                            # down. Re-run the calibrator before changing this.
BELOW_HI = 0.55             # >= this fraction below VWAP -> bearish lean
BELOW_LO = 0.45             # <= this -> bullish lean
STOP_BUF = 3.0              # pts beyond the guard pivot for the stop
MAX_DIR_ENTRIES = 2         # directional pivot entries per day (avoid overtrading)


class PivotStrategy(BaseStrategy):
    def __init__(self, symbol: str, point_usd: float = 50.0, gamma=None,
                 base_size: int = 1) -> None:
        self.symbol = symbol
        self.point_usd = point_usd
        self.gamma = gamma                 # optional GammaRegime (conviction only)
        # fixed size: pivot-to-pivot stops are far too wide to risk-size against
        # a small budget, and the user trades a fixed lot (loose stop, covers at
        # the target pivot / EOD). conviction can scale this later.
        self.base_size = base_size
        self.pos = 0
        self._day: str | None = None
        self.mp = MultiPivots()                 # day + week + month floor pivots
        self._reset_session()

    # ── per-session state ────────────────────────────────────────────────────
    def _reset_session(self) -> None:
        self.grid: list[float] = []             # merged D/W/M pivot prices, sorted
        self._grid_sig: tuple | None = None     # rebuild when the PERIODS change
        self.labels: dict[float, str] = {}      # price -> 'D-S2' / 'W-PP' / 'M-R1'
        self._open: float | None = None         # session open (first RTH bar)
        self._prior_range = 0.0                 # prior-DAY range (deep-reversal scale)
        # overnight bias accumulators (session start -> US open)
        self._on_closes: list[float] = []
        self._cum_pv = 0.0
        self._cum_v = 0.0
        self._below = 0
        self._on_n = 0
        self.bias = 0                            # -1/0/+1, set at US open
        self.conviction = 0.0
        self._bias_done = False
        # trade state
        self.trade: dict | None = None           # {dir, entry, target, stop, tag}
        self._used: set[float] = set()           # pivots already entered from
        self._dir_entries = 0
        self._reversal_used = False

    def on_position(self, p) -> None:
        self.pos = p.qty

    def seed_history(self, bars) -> None:
        """Prime the D/W/M pivot periods from historical (e.g. daily) bars so the
        weekly/monthly grid is correct from the FIRST live session, instead of
        taking a week/month of live bars to fill. Feeds the period tracker only;
        emits nothing. Call once, before the engine runs.

        These bars are already whole sessions, so they bypass the RTH filter that
        live intraday bars go through. That makes their PROVENANCE critical: they
        must be RTH aggregates of the traded contract. Seeding from a 24h
        continuous series put the 2026-08-07 daily low 18 points wrong."""
        for b in bars:
            self.mp.update(b.ts, b.h, b.l, b.c, rth_only=False)

    def reset_for_live(self) -> None:
        self.trade = None
        self.pos = 0

    # ── the fuzzy part: the overnight directional read ──────────────────────
    def _compute_bias(self) -> None:
        self._bias_done = True
        n = self._on_n
        if n < 30 or self._cum_v <= 0:           # not enough overnight -> stand down
            self.bias, self.conviction = 0, 0.0
            return
        closes = self._on_closes
        net = closes[-1] - closes[0]
        churn = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
        er = abs(net) / churn if churn > 0 else 0.0      # Kaufman ER of the night
        # normalize against the random-walk baseline for THIS bar count, so the
        # threshold means "x times more directional than noise" (see RW_C above)
        er = er * (len(closes) ** 0.5) / RW_C
        below_frac = self._below / n
        if er < ER_MIN_NORM:                     # choppy night = "just wiggle"
            self.bias, self.conviction = 0, er
            return
        if below_frac >= BELOW_HI and net < 0:
            self.bias = -1                       # mostly below VWAP + trending down
        elif below_frac <= BELOW_LO and net > 0:
            self.bias = +1
        else:
            self.bias = 0
        self.conviction = er
        # short gamma amplifies conviction (dealers add to the move)
        if self.bias != 0 and self.gamma is not None:
            try:
                if self.gamma.is_short_gamma(self._day):
                    self.conviction = er * 1.5   # normalized scale: no 1.0 cap
            except Exception:                    # noqa: BLE001
                pass

    # ── main ────────────────────────────────────────────────────────────────
    def on_bar(self, bar: Bar) -> list[Order]:
        if bar.tf != "1m":
            return []
        day = et_session_date(bar.ts)
        self.mp.update(bar.ts, bar.h, bar.l, bar.c)     # track D/W/M H/L/C (RTH only)
        if day != self._day:                     # new session -> reset trade state
            self._day = day
            self._reset_session()
        # The grid follows the PERIOD ROLL, not the calendar day. Now that
        # MultiPivots ignores overnight bars, the daily period rolls at the first
        # RTH bar -- while et_session_date rolls at ET midnight. Keying the
        # rebuild to the calendar day therefore ran it BEFORE the period had
        # rolled, and the grid stayed empty for the entire session.
        sig = tuple(sorted(self.mp.prior.items()))
        if sig != self._grid_sig:
            self._grid_sig = sig
            self.labels = self.mp.grid()
            self.grid = sorted(self.labels)
            pr = self.mp.prior.get("D")
            self._prior_range = (pr[0] - pr[1]) if pr else 0.0

        m = et_minute_of_day(bar.ts)
        if m < RTH_START:                        # overnight: accumulate the bias read
            v = float(bar.v) if bar.v else 0.0
            self._cum_v += v
            self._cum_pv += bar.c * v
            vwap = self._cum_pv / self._cum_v if self._cum_v > 0 else bar.c
            self._on_closes.append(bar.c)
            self._on_n += 1
            if bar.c < vwap:
                self._below += 1
            return []
        if not self._bias_done:                  # first RTH bar -> freeze the bias
            self._compute_bias()
        if self._open is None:
            self._open = bar.o
        if not self.grid:
            return []
        if m >= EOD_FLAT:                         # flat by the close
            return self._flatten("eod")
        if self.pos != 0 and self.trade is not None:
            return self._manage(bar)
        if self.pos == 0 and self.bias != 0:
            return self._scan(bar)
        return []

    # ── entries ──────────────────────────────────────────────────────────────
    def _scan(self, bar: Bar) -> list[Order]:
        d = self.bias
        # DEEP-pivot reversal: the deepest grid pivot beyond the open by >= a
        # volatility margin — "far enough down for a bounce" — from ANY timeframe
        # (a weekly/monthly support is often the real floor, not the daily one).
        margin = max(10.0, 0.3 * self._prior_range)
        if self._open is not None and not self._reversal_used:
            if d < 0:
                deep = [p for p in self.grid if p <= self._open - margin]
                rev_lvl = max(deep) if deep else None    # first deep level reached
                touched = rev_lvl is not None and bar.l <= rev_lvl
            else:
                deep = [p for p in self.grid if p >= self._open + margin]
                rev_lvl = min(deep) if deep else None
                touched = rev_lvl is not None and bar.h >= rev_lvl
            if touched:
                self._reversal_used = True
                rdir = -d                        # reversal trades AGAINST the bias
                tgt = self._cum_pv / self._cum_v if self._cum_v > 0 else self._open
                stop = rev_lvl - rdir * (2 * STOP_BUF)   # just beyond the deep pivot
                return self._enter(rdir, rev_lvl, tgt, stop, "pivrev")
        # DIRECTIONAL fade: price retraces INTO the nearest pivot against the
        # move (short bias -> nearest pivot ABOVE; long bias -> nearest BELOW).
        if self._dir_entries >= MAX_DIR_ENTRIES:
            return []
        if d < 0:
            cands = [p for p in self.grid if p >= bar.c and p not in self._used]
            piv = min(cands) if cands else None
            hit = piv is not None and bar.h >= piv
        else:
            cands = [p for p in self.grid if p <= bar.c and p not in self._used]
            piv = max(cands) if cands else None
            hit = piv is not None and bar.l <= piv
        if hit:
            self._used.add(piv)
            self._dir_entries += 1
            below = [p for p in self.grid if p < piv]
            above = [p for p in self.grid if p > piv]
            if d < 0:                            # short: target next pivot down
                tgt = max(below) if below else piv - 2 * STOP_BUF
                stop = (min(above) if above else piv + 2 * STOP_BUF) + STOP_BUF
            else:                                # long: target next pivot up
                tgt = min(above) if above else piv + 2 * STOP_BUF
                stop = (max(below) if below else piv - 2 * STOP_BUF) - STOP_BUF
            return self._enter(d, piv, tgt, stop, "piv")
        return []

    def _enter(self, d: int, entry: float, target: float, stop: float,
               tag: str) -> list[Order]:
        size = self.base_size
        if size <= 0:
            return []
        self.trade = {"dir": d, "entry": entry, "target": target, "stop": stop,
                      "size": size, "tag": tag}
        # A LIMIT AT THE LEVEL, not a market order. The docstring has always said
        # "resting limit-style entries at pivots"; the code sprayed at the market
        # and then recorded `entry` = the pivot anyway, so stop and target were
        # derived from a price the trade never had. Measured on 5 ES sessions:
        # piv-entry fills sat a median 6.67 points off the nearest level, one of
        # them 21.75 -- which is not a fade, and a stop of piv + STOP_BUF taken
        # 21 points away is not the risk it claims.
        #
        # The touch test that got us here (bar.h >= piv short, bar.l <= piv long)
        # is exactly the condition under which a limit resting at `piv` fills, so
        # the level was reachable on this bar by construction.
        return [Order(self.symbol, d, size, type=OrderType.LIMIT,
                      limit_price=entry, tag=f"{tag}-entry")]

    # ── management ───────────────────────────────────────────────────────────
    def _manage(self, bar: Bar) -> list[Order]:
        t = self.trade
        d = t["dir"]
        target_hit = bar.h >= t["target"] if d > 0 else bar.l <= t["target"]
        stop_hit = bar.l <= t["stop"] if d > 0 else bar.h >= t["stop"]
        if target_hit:
            return self._flatten("target")
        if stop_hit:
            return self._flatten("stop")
        return []

    def _flatten(self, why: str) -> list[Order]:
        if self.pos == 0 or self.trade is None:
            self.trade = None
            return []
        d = self.trade["dir"]
        qty = abs(self.pos)
        self.trade = None
        return [Order(self.symbol, -d, qty, tag=f"piv-{why}", reduce_only=True)]


__all__ = ["PivotStrategy"]
