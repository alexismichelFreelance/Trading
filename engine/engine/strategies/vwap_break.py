"""VwapBreakStrategy — trade the BREAK of the RTH session-VWAP band, WITH the
break (continuation), not against it.

Motivated by tools/avwap_study.py: on the ES sample, the session-open VWAP is
the one anchor with a *significant* reaction — and it is NEGATIVE. Price that
tests session VWAP tends to break THROUGH it (~+0.7pt/15m of continuation),
not revert. So the honest way to trade session VWAP is the break, not the fade.

The danger is chop: VWAP gets crossed many times on a balance day, and fading
or chasing every re-cross bleeds. Three guards:
  1. Require a close beyond the +/- k*sigma VWAP band (a genuine expansion, not
     a wiggle across the line) — sigma is the volume-weighted dispersion from
     AnchoredVWAP.
  2. AND require a new intraday extreme close, so mid-range pokes through the
     (small, early) band don't qualify — only real breakouts do.
  3. Bail the instant price falls back to the VWAP mean (continuation failed).
Gamma-aware (opt-in): breaks RUN on short-gamma days (dealers add to the move)
and FAIL on long-gamma days, so entries gate to short gamma. One position at a
time, two entries/day max, flat at 15:59 ET.

NOT a validated edge — a study-motivated sleeve that paper-trades alongside the
rest until it earns (or fails to earn) a live slot.
"""
from __future__ import annotations

from ..core.events import Bar
from ..core.exits import ExitCtx, TwoPhaseExit
from ..core.orders import Order, OrderType
from ..core.timeutil import et_minute_of_day, et_session_date
from ..features.avwap import AnchoredVWAP
from .base import BaseStrategy

RTH_START = 9 * 60 + 30      # 09:30 — RTH open
GLOBEX_OPEN = 18 * 60        # 18:00 ET — the futures session open NT8 anchors to
START_MIN = 10 * 60          # 10:00 — no entries before this (sigma must settle)
EOD_FLAT = 15 * 60 + 59      # 15:59 — flat everything
MAX_ENTRIES = 2              # a failed break then a real one is common
OR_END = 10 * 60             # 10:00 ET — the opening range is 09:30-10:00
REGIME_CLOSES = 3            # 5m closes the far side of VWAP = the other side is in control


class VwapBreakStrategy(BaseStrategy):
    def __init__(self, symbol: str, *, band_k: float = 1.0,
                 stop_mult: float = 1.0, trail_mult: float = 1.5,
                 stop_floor: float = 5.0, trail_floor: float = 8.0,
                 gamma=None, two_phase: TwoPhaseExit | None = None,
                 entry_mode: str = "market", retest_ttl: int = 30,
                 anchor: str = "globex", retest_tol_frac: float = 0.0,
                 qual_ref: str = "or", qual_extension: bool = True,
                 qual_clean: bool = True) -> None:
        """entry_mode:
          'market'  — take the break at the bar close that made it (original).
          'retest'  — do NOT chase. Arm on the break and rest a LIMIT at the
                      VWAP line; it fills only if price comes back to touch it.

        The retest variant exists because the break is repeatedly bought at the
        worst price of the move and the line gets touched again afterwards. It
        needs its own invalidation: `market` exits when price returns to VWAP,
        which an entry filled AT VWAP would trigger on its own fill. So a retest
        trade is dead when price closes back through VWAP by more than a
        fraction of the band, not on the touch itself.

        retest_ttl: bars the resting limit stays armed before the setup is
        considered stale (0 = never expires)."""
        self.symbol = symbol
        self.entry_mode = entry_mode
        self.retest_ttl = retest_ttl
        # "at the pullback at VWAP (OR CLOSE TO IT)". 0.0 = an exact touch of the
        # line; >0 accepts a fill within this fraction of the setup's width.
        # Requiring an exact touch after a full-range extension is a very deep
        # retracement: 94 qualified breaks produced 4 fills on 38 ES sessions.
        self.retest_tol_frac = retest_tol_frac
        # ABLATION KNOBS. The spec bundles three conditions and the bundle loses;
        # these separate them so the question "which condition is worth keeping"
        # has an answer instead of an opinion.
        #   qual_ref        "or"   break the OPENING RANGE (the spec)
        #                   "band" break the VWAP +/- k*sigma band (what we do)
        #   qual_extension  require one full width past the break
        #   qual_clean      require no close back through VWAP on the way
        self.qual_ref = qual_ref
        self.qual_extension = qual_extension
        self.qual_clean = qual_clean
        # WHICH VWAP. 'globex' = NT8's VWAPX as the user runs it: reset at
        # MIDNIGHT ON THE CHART CLOCK, calculated on bar close. The chart clock
        # is UTC+2, so midnight there is 18:00 ET -- verified against the tape on
        # 2026-08-05: this line falls inside the 10:30 ET bar (7799.25-7804.25 vs
        # 7803.07) and within 1.2 ticks of the 10:35 bar. Anchoring anywhere
        # later (00:00 ET, 01:00 CT, 09:30 RTH) puts it 2-7 points HIGHER and
        # matches nothing. 'rth' anchors at 09:30 and is kept only for the old
        # study comparison.
        #
        # rth was the original default and it was never a choice: it came from
        # tools/avwap_study.py, whose own caveats say "RTH only (no Globex...)".
        # That study could not test a Globex anchor because its dataset had no
        # overnight bars, so rth_open won by being the only session anchor
        # present -- and the docstring then reported it as the anchor that
        # works. On 2026-08-05 the two lines sat 6 POINTS apart: price touched
        # the Globex VWAP at 10:30 and 10:35 ET while the RTH line was never
        # within 6 points all morning. The sleeve was trading a line that does
        # not exist on the chart.
        self.anchor = anchor
        # ── qualified-break state (entry_mode="qualified") ───────────────────
        # The user's rule, which is NOT what market/retest do:
        #   1 break the OPENING RANGE (not a sigma band)
        #   2 extend one full RANGE WIDTH past the break (not "a new session
        #     extreme close", which a single tick satisfies)
        #   3 no close back through VWAP on the way -- the move must be clean
        #   4 then enter on the pullback to VWAP
        # and 3 consecutive 5m closes the far side of VWAP flip the regime.
        self.or_hi: float | None = None
        self.or_lo: float | None = None
        self._broke: dict | None = None     # break seen, extension not yet met
        self._qualified: dict | None = None  # all three conditions met
        self._regime = 0                     # +1 above, -1 below, 0 undecided
        self._run_below = 0                  # consecutive 5m closes below VWAP
        self._run_above = 0
        self._5m_bucket: int | None = None
        self._5m_close: float | None = None
        # counters: _qualified is CONSUMED into _armed on the same bar, so it
        # cannot be observed after the fact. These survive.
        self.n_broke = 0        #条件1 met: opening range broken
        self.n_qualified = 0    # conditions 1-3 all met
        self.n_disqualified = 0  # broke, then closed back through VWAP
        self.gamma = gamma
        self.band_k = band_k
        self.stop_mult, self.trail_mult = stop_mult, trail_mult
        self.stop_floor, self.trail_floor = stop_floor, trail_floor
        self.two_phase = two_phase
        self._day: str | None = None
        self.pos = 0
        self._reset_session()

    def _reset_session(self) -> None:
        self.av = AnchoredVWAP()                  # VWAPX: bar close x bar volume
        self.sess_hi: float | None = None         # session high/low CLOSE so far
        self.sess_lo: float | None = None
        self.entries = 0
        self.trade: dict | None = None            # {side, entry, stop, trail, peak}
        # retest mode: a limit is resting at the VWAP line, not yet filled
        self._armed: dict | None = None           # {side, px, bars, stop, trail}
        self.or_hi = self.or_lo = None
        self._broke = self._qualified = None
        self._regime = 0
        self._run_below = self._run_above = 0
        self._5m_bucket = self._5m_close = None

    def on_position(self, p) -> None:
        self.pos = p.qty

    def reset_for_live(self) -> None:
        self.trade = None
        self._armed = None
        self.pos = 0

    # ── main ─────────────────────────────────────────────────────────────────
    def _session_key(self, ts: int) -> str:
        """Session identity for the VWAP anchor. For 'globex', 18:00 ET starts
        the NEXT session -- et_session_date rolls at ET midnight, which would
        otherwise cut the line in half every evening."""
        day = et_session_date(ts)
        if self.anchor == "globex" and et_minute_of_day(ts) >= GLOBEX_OPEN:
            import pandas as _pd
            day = (_pd.Timestamp(day) + _pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        return day

    def on_bar(self, bar: Bar) -> list[Order]:
        if bar.tf != "1m":
            return []
        day = self._session_key(bar.ts)
        if day != self._day:
            self._day = day
            self._reset_session()
            if self.pos != 0:                     # never carry overnight
                side = 1 if self.pos > 0 else -1
                return [Order(self.symbol, -side, abs(self.pos), tag="safety-flat",
                              reduce_only=True)]
        m = et_minute_of_day(bar.ts)
        # The VWAP accumulates across the WHOLE session (from 18:00 ET for the
        # globex anchor); only ENTRIES are gated to RTH below.
        if self.anchor == "rth" and m < RTH_START:
            return []
        if self.two_phase is not None:
            self.two_phase.note_price(bar.c, bar.ts)
        # accumulate the RTH session VWAP on this bar's typical price
        # "calculate on bar close": the CLOSE of each completed bar, weighted by
        # its volume -- not the (h+l+c)/3 typical price the sleeve used before.
        self.av.add(bar.c, float(bar.v) if bar.v else 0.0)
        if m < RTH_START or m >= EOD_FLAT + 1:     # outside RTH: accumulate only
            return []
        if m >= EOD_FLAT:
            return self._flatten("moc")
        vwap, sigma = self.av.value, self.av.sigma
        up = vwap + self.band_k * sigma
        lo = vwap - self.band_k * sigma
        if self.entry_mode == "qualified":
            return self._qualified_bar(bar, m, vwap)
        orders: list[Order] = []
        if self.pos != 0 and self.trade is not None:
            orders = self._manage(bar.c, vwap)
        elif self._armed is not None:
            # a limit is resting at the line: age it, and fill it the moment the
            # bar's RANGE contains the level (that is what a resting limit does)
            orders = self._check_retest(bar, vwap)
        elif (self.pos == 0 and self.entries < MAX_ENTRIES and m >= START_MIN
              and sigma > 0 and self.sess_hi is not None):
            orders = self._maybe_enter(bar.ts, bar.c, up, lo)
        # track session extreme CLOSES (updated AFTER the break check)
        self.sess_hi = bar.c if self.sess_hi is None else max(self.sess_hi, bar.c)
        self.sess_lo = bar.c if self.sess_lo is None else min(self.sess_lo, bar.c)
        return orders

    # ── entry: break of the band that is ALSO a new session extreme ──────────
    def _maybe_enter(self, ts: int, c: float, up: float, lo: float) -> list[Order]:
        # a genuine expansion: beyond the +/-k*sigma band AND a fresh intraday
        # extreme (not a mid-range wiggle across the line — the chop that bleeds).
        if c > up and c > self.sess_hi:
            side = 1
        elif c < lo and c < self.sess_lo:
            side = -1
        else:
            return []
        if not self.gamma_entry_ok(ts, "short"):          # continuation needs short gamma
            return []
        w = max(1e-9, up - lo)                             # band width = 2*k*sigma
        stop = max(self.stop_floor, self.stop_mult * w)
        trail = max(self.trail_floor, self.trail_mult * w)
        if self.entry_mode == "retest":
            # do not chase the break. Arm; the fill happens at the VWAP line if
            # and only if price comes back to it.
            self._armed = {"side": side, "bars": 0, "stop": stop, "trail": trail,
                           "band": w}
            return []
        self.trade = {"side": side, "entry": c, "stop": stop, "trail": trail,
                      "peak": 0.0, "band": w}
        if self.two_phase is not None:
            self.two_phase.start(side, c)
        self.entries += 1
        return [Order(self.symbol, side, 1, tag="entry-vwapbreak")]

    # ── qualified break (the user's rule) ────────────────────────────────────
    def _note_5m_close(self, below: bool) -> None:
        """One CLOSED 5m bar, relative to VWAP. Three consecutive on the far
        side hand control to that side. Consecutive means consecutive: a close
        back on the other side resets the count, it does not merely pause it."""
        if below:
            self._run_below += 1
            self._run_above = 0
            if self._run_below >= REGIME_CLOSES:
                self._regime = -1
        else:
            self._run_above += 1
            self._run_below = 0
            if self._run_above >= REGIME_CLOSES:
                self._regime = 1

    def _apply_regime(self, below: bool, price: float) -> list[Order]:
        """Feed a 5m close and close any position the new regime is against."""
        was = self._regime
        self._note_5m_close(below)
        if self._regime and self._regime != was and self.trade is not None                 and self.trade["side"] != self._regime:
            return self._flatten("regime-flip")
        return []

    def _qualified_bar(self, bar: Bar, m: int, vwap: float) -> list[Order]:
        # 1) the opening range, 09:30-10:00
        if m < OR_END:
            self.or_hi = bar.h if self.or_hi is None else max(self.or_hi, bar.h)
            self.or_lo = bar.l if self.or_lo is None else min(self.or_lo, bar.l)
            return []
        if self.or_hi is None or self.or_lo is None:
            return []
        if self.qual_ref == "band":
            sigma = self.av.sigma
            if sigma <= 0:
                return []
            ref_hi, ref_lo = vwap + self.band_k * sigma, vwap - self.band_k * sigma
        else:
            ref_hi, ref_lo = self.or_hi, self.or_lo
        width = ref_hi - ref_lo
        if width <= 0:
            return []
        out: list[Order] = []
        # 5m regime accounting on CLOSED 5m buckets
        bucket = m // 5
        if self._5m_bucket is None:
            self._5m_bucket = bucket
        elif bucket != self._5m_bucket:
            if self._5m_close is not None:
                out += self._apply_regime(self._5m_close < vwap, bar.c)
            self._5m_bucket = bucket
        self._5m_close = bar.c

        if self.pos != 0 and self.trade is not None:
            return out + self._manage(bar.c, vwap)
        if self._armed is not None:
            return out + self._check_retest(bar, vwap)

        # 3) cleanliness: a close back through VWAP kills a pending break
        if self._broke is not None and self.qual_clean:
            side = self._broke["side"]
            if (side > 0 and bar.c < vwap) or (side < 0 and bar.c > vwap):
                self._broke = None
                self.n_disqualified += 1
        # 1) break of the opening range
        if self._broke is None and self._qualified is None:
            if bar.c > ref_hi:
                self._broke = {"side": 1, "target": ref_hi + width}
                self.n_broke += 1
            elif bar.c < ref_lo:
                self._broke = {"side": -1, "target": ref_lo - width}
                self.n_broke += 1
        # 2) extension of one full range width past the break
        if self._broke is not None and self._qualified is None:
            side, tgt = self._broke["side"], self._broke["target"]
            reached = (bar.h >= tgt) if side > 0 else (bar.l <= tgt)
            if reached or not self.qual_extension:
                self._qualified = dict(self._broke)
                self.n_qualified += 1
                self._broke = None
        # 4) enter on the pullback to VWAP
        if (self._qualified is not None and self.pos == 0
                and self.entries < MAX_ENTRIES and self.trade is None):
            side = self._qualified["side"]
            if self._regime == 0 or self._regime == side:
                stop = max(self.stop_floor, self.stop_mult * width)
                self._armed = {"side": side, "bars": 0, "stop": stop,
                               "trail": max(self.trail_floor, self.trail_mult * width),
                               "band": width}
                self._qualified = None
        return out

    # ── retest: the resting limit at the VWAP line ───────────────────────────
    def _check_retest(self, bar: Bar, vwap: float) -> list[Order]:
        a = self._armed
        a["bars"] += 1
        if self.retest_ttl and a["bars"] > self.retest_ttl:
            self._armed = None                    # setup went stale, never filled
            return []
        # a resting limit fills when the tape trades through its level; on bar
        # data that is exactly "the bar's range contains the line".
        # "(or close to it)": the tolerance must move the LIMIT PRICE, not just
        # the trigger. Placing the order AT vwap after merely coming NEAR vwap
        # arms a level price never reaches -- it filled 0 of 36 sessions in the
        # replay while a bar-only count said 12, because the count scored the
        # order rather than the fill.
        tol = self.retest_tol_frac * a.get("band", 0.0)
        side = a["side"]
        level = vwap + tol if side > 0 else vwap - tol   # near side of the line
        if not (bar.l <= level <= bar.h):
            return []
        self._armed = None
        self.trade = {"side": side, "entry": level, "stop": a["stop"],
                      "trail": a["trail"], "peak": 0.0, "band": a["band"]}
        if self.two_phase is not None:
            self.two_phase.start(side, level)
        self.entries += 1
        return [Order(self.symbol, side, 1, type=OrderType.LIMIT,
                      limit_price=level, tag="entry-vwapretest")]

    # ── management: bail to VWAP (thesis dead) or hard/trailing stop ─────────
    def _manage(self, c: float, vwap: float) -> list[Order]:
        t = self.trade
        side = t["side"]
        # continuation failed the moment price returns to the mean. A RETEST
        # entry is filled AT the mean, so the bare touch is its own entry, not a
        # failure: it is dead only once price closes THROUGH the line against the
        # position by a fraction of the band.
        if self.entry_mode == "retest":
            slack = 0.25 * t.get("band", 0.0)
            if (side > 0 and c < vwap - slack) or (side < 0 and c > vwap + slack):
                return self._flatten("vwap-fail")
        elif (side > 0 and c <= vwap) or (side < 0 and c >= vwap):
            return self._flatten("vwap-fail")
        fe = (c - t["entry"]) * side
        t["peak"] = max(t["peak"], fe)
        if self.two_phase is not None:
            # 2026-07-28: this sleeve ran +127.75pt and closed at -14.50 on the
            # MOC, 142.25pt given back. Ride, then hunt the reversal.
            hit = self.two_phase.check(ExitCtx(ts=0, price=c, dir=side,
                                               entry_px=t["entry"], entry_ts=0,
                                               peak_fe=t["peak"]))
            if hit:
                return self._flatten(hit)
            return self._flatten("stop") if fe <= -t["stop"] else []
        if fe <= max(-t["stop"], t["peak"] - t["trail"]):
            return self._flatten("trail")
        return []

    def _flatten(self, why: str) -> list[Order]:
        if self.pos == 0 or self.trade is None:
            self.trade = None
            return []
        side = self.trade["side"]
        qty = abs(self.pos)
        self.trade = None
        return [Order(self.symbol, -side, qty, tag=f"vwb-{why}", reduce_only=True)]


__all__ = ["VwapBreakStrategy"]
