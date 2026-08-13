"""ZoneLifecycleStrategy — event-driven, single-position (live/replay engine form).

Detects 30m S/D zones and trades the three setups (fade / break / flip) with the
half-off-at-+4 breakeven-runner scale-out. The SAME decision logic as
zones_oracle.py (the parity gate); this version runs one position at a time
through the engine and manages exits against each closed 30m bar, so its P&L is
the realistic single-position number (lower than the per-signal oracle's +$47.5k).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.events import Bar
from ..core.orders import Order, OrderType
from ..core.timeutil import et_minute_of_day, et_session_date, ns_to_utc
from ..features.bars import BarAggregator
from ..features.zones import ZoneDetector
from .base import BaseStrategy, level_fill
from .sizing import position_size

SCALP = 4.0
K_BARS = 16
RISK = 2000.0
RTH_OPEN_MIN, RTH_CLOSE_MIN = 9 * 60 + 30, 16 * 60   # entries only inside RTH


# ── zone metadata on the fill ────────────────────────────────────────────────
# The detector scores every zone (departure 0-2 + base 0-2) and counts touches,
# then the sleeve threw all of it away: the fill said "fade-entry" and nothing
# else. So the trade record could not answer whether strong, untouched zones
# outperform weak re-tested ones -- across 36 ES sessions that was unknowable
# rather than negative, and no replay could recover it.
#
# Stamped at entry, at the moment of the decision. The setup prefix is kept
# first so every existing reader of "fade-entry" keeps working.
#     fade-entry|d2|b1|t0|30m

def zone_tag(setup: str, dep: int, base: int, touches: int, tf: str) -> str:
    return f"{setup.lower()}-entry|d{int(dep)}|b{int(base)}|t{int(touches)}|{tf}"


def parse_zone_tag(tag):
    """{setup, dep, base, strength, touches, virgin, tf} or None if the tag
    carries no zone metadata (every fill written before this, and every exit)."""
    if not tag or "|" not in str(tag):
        return None
    parts = str(tag).split("|")
    if len(parts) != 5 or not parts[0].endswith("-entry"):
        return None
    try:
        dep = int(parts[1][1:]); base = int(parts[2][1:]); tch = int(parts[3][1:])
    except ValueError:
        return None
    return {"setup": parts[0][:-len("-entry")], "dep": dep, "base": base,
            "strength": dep + base, "touches": tch, "virgin": tch == 0,
            "tf": parts[4]}


@dataclass
class _ZoneRec:
    k: int
    dir: int
    top: float
    bot: float
    fade_done: bool = False
    broke: bool = False
    break_k: int | None = None
    flip_done: bool = False
    ts: int = 0                    # creation time (ns) — metadata for chart painting
    dep: int = 0                   # departure_score 0-2, from the detector
    base_s: int = 0                # base_score 0-2
    touches: int = 0               # times price has entered the band
    is_gap: bool = False           # RTH-open gap zone
    armed: bool = True             # gap zones start disarmed until price leaves them

    @property
    def prox(self) -> float:
        return self.top if self.dir > 0 else self.bot


@dataclass
class _Trade:
    setup: str
    dir: int
    entry: float
    stop: float
    target: float
    size: int
    scalp_px: float
    scalped: bool = False
    remaining: int = 0
    bars_left: int = K_BARS


class ZoneLifecycleStrategy(BaseStrategy):
    def __init__(self, symbol: str,
                 gate_utc: tuple[int, int] | None = (13, 21),
                 gap_thr: float = 0.0, point_usd: float = 50.0,
                 tf: str = "30m", enable_break: bool = True,
                 runner: bool = True) -> None:
        self.symbol = symbol
        self.point_usd = point_usd     # $/pt for sizing (from InstrumentSpec)
        # 30m was chosen on ES history and is the validated default -- do not
        # change it for ES without redoing that work. It is a parameter because
        # nothing says the same bucket suits an instrument whose median RTH range
        # is 6.5x larger: a 30m NQ zone is ~6.5x wider in points than a 30m ES
        # zone, which is a different trade, not the same one scaled.
        self.tf = tf
        self.agg = BarAggregator((tf,))
        # Defaults keep the validated behaviour so tests/parity stays valid; the
        # roster turns these off on 36 ES sessions of evidence.
        #   BREAK setup   4 trips  -1,938  25% win   (fade +6,412/86%, flip +7,288/100%)
        #   the RUNNER    4 trips  -2,100  25% win   -- half comes off at +4 and
        #     what is left, held to breakeven, gives back more than it makes.
        self.enable_break = enable_break
        self.runner = runner
        # gap_thr>0: also detect RTH-open gap zones, faded only after a
        # leave-and-return (naive immediate-fade lost -$36k; see
        # strategy_lab/GAP_DEPARTURE_STUDY.md). Default 0 = base-only.
        self.det = ZoneDetector(gap_thr=gap_thr)
        self.zones: list[_ZoneRec] = []
        self._k = -1
        self.pos = 0
        self._day: str | None = None
        self.trade: _Trade | None = None
        # NEW entries only inside the validated window; management always runs
        self.gate_utc = gate_utc

    def on_bar(self, bar: Bar) -> list[Order]:
        # SESSION BOUNDARY. This sleeve had none: no session date, no daily
        # reset, no end-of-day flat. Its only exit bound was K_BARS bars, so a
        # late entry rode through the close, through the 17:00 ET Globex halt and
        # into the next session -- while carrying the LARGEST position in the
        # roster (it is the only risk-sized sleeve, 5+ contracts).
        day = et_session_date(bar.ts)
        if day != self._day:
            self._day = day
            self.trade = None            # a new session starts flat and fresh
        if self.session_over(bar.ts) and self.pos != 0:
            d = 1 if self.pos > 0 else -1
            qty, self.trade = abs(self.pos), None
            return [Order(self.symbol, -d, qty, tag="session-flat", reduce_only=True)]
        # RTH-only DETECTION (matches the validated claude_bars_1m window + the
        # methodology). Live feeds carry overnight bars; without this the live 30m
        # zones diverge from the backtest.
        # Expressed in ET, not UTC hours: 13-21 UTC is 09:00-17:00 ET in summer
        # but 08:00-16:00 ET in winter, so the detection window silently shifted
        # by an hour at each DST change. The 2025 parity data is all EDT, so this
        # is identical there.
        m = et_minute_of_day(bar.ts)
        if not (9 * 60 <= m < 17 * 60):
            return []
        orders: list[Order] = []
        for b in self.agg.update(bar):
            orders += self._on_tf_bar(b)
        return orders

    def on_position(self, p) -> None:
        self.pos = p.qty

    def reset_for_live(self) -> None:
        # keep detected zones (market structure), drop the phantom warmup trade
        # and RE-ARM every still-valid zone so live touches fade/break cleanly
        self.trade = None
        self.pos = 0
        for z in self.zones:
            if not z.broke:
                z.fade_done = False
                z.flip_done = False

    # ── per closed 30m bar ───────────────────────────────────────────────
    def _on_tf_bar(self, b: Bar) -> list[Order]:
        self._k += 1
        orders: list[Order] = []
        if self.trade is not None:
            orders += self._manage(b)
        for z in self.det.update(b):
            self.zones.append(_ZoneRec(self._k, z.direction, z.top, z.bot, ts=b.ts,
                                       dep=z.departure_score, base_s=z.base_score,
                                       is_gap=z.is_gap, armed=not z.is_gap))
        in_window = self.gate_utc is None or \
            (self.gate_utc[0] <= ns_to_utc(b.ts).hour < self.gate_utc[1])
        if self.trade is None and self.pos == 0 and in_window:
            orders += self._scan(b)
        # LEAVE-AND-RETURN arming (after scan, so a gap fade needs a PRIOR bar to
        # have left the zone): a gap zone arms once price clears its proximal edge
        # touch accounting -- `virgin` must mean something by the time a later
        # bar takes the zone, so count entries into the band on every bar
        for z in self.zones:
            if not z.broke and b.l <= z.top and b.h >= z.bot:
                z.touches += 1
        for z in self.zones:
            if z.is_gap and not z.armed and not z.broke:
                if (b.h > z.top) if z.dir > 0 else (b.l < z.bot):
                    z.armed = True
        return orders

    def _entry_window_open(self, ts: int) -> bool:
        """RTH, in ET MINUTES.

        This was `gate_utc[0] <= ns_to_utc(ts).hour < gate_utc[1]` with (13, 21)
        -- UTC HOURS. 13:00 UTC is 09:00 ET in summer and 08:00 ET in winter, so
        the entry window ran half an hour before the open, an hour past the
        close, and slid by an hour at every DST change while the session stayed
        put. The identical bug was fixed in engine/painters.py on 2026-08-06 and
        left here, in the copy that actually places orders.

        gate_utc=None still disables the gate entirely.
        """
        if self.gate_utc is None:
            return True
        m = et_minute_of_day(ts)
        return RTH_OPEN_MIN <= m < RTH_CLOSE_MIN

    def _opp_target(self, want_dir: int, price: float, before_k: int, dir_sign: int) -> float | None:
        cands = [zz for zz in self.zones if zz.dir == want_dir and zz.k < before_k
                 and (zz.bot > price + 3 if dir_sign > 0 else zz.top < price - 3)]
        if not cands:
            return None
        return min(z.bot for z in cands) if dir_sign > 0 else max(z.top for z in cands)

    def _enter(self, setup: str, d: int, entry: float, stop: float, target: float,
               z: "_ZoneRec | None" = None, bar: Bar | None = None) -> list[Order]:
        risk = abs(entry - stop)
        size = position_size(RISK, risk, self.point_usd, 30)
        if size <= 0:
            return []
        self.trade = _Trade(setup, d, entry, stop, target, size,
                            entry + d * SCALP, remaining=size)
        tag = (zone_tag(setup, z.dep, z.base_s, z.touches, self.tf) if z is not None
               else f"{setup.lower()}-entry")
        # PRICED AT THE EDGE. This recorded `entry` -- the zone edge -- and then
        # sprayed at the market, so stop, target, scalp_px and the post-scalp
        # breakeven stop were all measured from a price the trade never had. It
        # showed up as an impossibility: over 25 ES sessions ES:zones_gap booked
        # 26 BREAKEVEN exits worth -12,225, a mean of -$470 each. `be` flattens
        # at t.stop = t.entry, so a breakeven exit that loses $470 is not a
        # result, it is the entry price being wrong.
        #
        # NOT a resting LIMIT, which is what I tried first and the replay threw
        # out: ES:zones and ES:zones_15m went from 9 and 12 trading days to ZERO,
        # zones_gap from 25 to 2. The touch that produces the signal happens
        # DURING the bar being closed, so a limit resting from the close onward
        # can only fill if price comes back -- on a 30m bar it does not. That
        # converts "enter at the level" into "enter on a re-touch", a different
        # strategy, not a fill-price correction. (Same mistake as the vwapbreak
        # retest bug: moving the ORDER instead of the PRICE.)
        #
        # The bar's own range contains the level, by the condition that fired:
        #     FADE d>0  b.l <= z.top      reached from above
        #     FLIP d>0  b.h >= z.bot      reached from below
        # so the trade happens on this bar, at that level -- exactly how a
        # resting order would have been filled inside it. `bar` also carries the
        # GAP case: an open already beyond the level means there was never a
        # print at it, and the open is the honest fill (base.level_fill).
        #
        # BREAK keeps no level at all: its entry is b.c, the close of the bar
        # that broke the zone. The entry price IS the market there, and claiming
        # a level would invent one the setup does not have.
        # ...but ONLY if the zone existed BEFORE this bar. `_on_tf_bar` appends
        # zones from det.update(b) and then calls _scan(b), so a zone can be
        # detected and faded on the same bar -- and a zone is created by its
        # DEPARTURE bar, whose extreme IS the edge. Pricing those at the edge is
        # lookahead: the level was not knowable until the bar closed, by which
        # time price was at the close. Measured on the recorded ES tape
        # (strategy_lab/zone_entry_lookahead.py): 8 of 33 entries are same-bar,
        # and all 8 are FADE -- two thirds of every fade the sleeve takes. Those
        # keep the market price, which is what they could actually have got.
        same_bar = z is not None and z.k >= self._k
        if setup == "BREAK" or bar is None or z is None or same_bar:
            return [Order(self.symbol, d, size, tag=tag)]
        # `d` here is the TRADE direction (for FLIP that is -z.dir, not the
        # zone's). In both setups a SHORT is entered at a level above, which
        # price rises into, and a LONG at a level below, which it falls into --
        # so the one test covers fade and flip and both zone directions.
        return [Order(self.symbol, d, size, tag=tag,
                      trigger_price=level_fill(bar, entry, rising=d < 0))]

    def _scan(self, b: Bar) -> list[Order]:
        for z in self.zones:
            d = z.dir
            # FADE — 1st touch of a fresh zone (gap zones only once armed: they
            # must have left and be RE-touched, not faded at the open)
            if not z.fade_done and not z.broke and z.armed:
                touch = (b.l <= z.top and b.l >= z.bot - 0.5) if d > 0 else (b.h >= z.bot and b.h <= z.top + 0.5)
                if touch:
                    z.fade_done = True
                    tgt = self._opp_target(-d, z.prox, self._k, d)
                    if tgt is None:
                        stop0 = z.bot - 1 if d > 0 else z.top + 1
                        tgt = z.prox + d * abs(z.prox - stop0) * 2
                    return self._enter("FADE", d, z.prox, z.bot - 1 if d > 0 else z.top + 1,
                                   tgt, z, b)
            # detect break
            if not z.broke and ((b.c < z.bot - 1) if d > 0 else (b.c > z.top + 1)):
                z.broke = True
                z.break_k = self._k
                bdir = -d
                b_entry = b.c
                b_stop = z.top + 1 if d > 0 else z.bot - 1
                tgt = self._opp_target(d, b_entry, self._k, bdir)
                if tgt is None:
                    tgt = b_entry + bdir * abs(b_entry - b_stop) * 2
                if self.enable_break:
                    return self._enter("BREAK", bdir, b_entry, b_stop, tgt, z, b)
            # FLIP — retest from broken side
            if z.broke and not z.flip_done and z.break_k is not None and self._k >= z.break_k + 2:
                ft = (b.h >= z.bot and b.h <= z.top + 0.5) if d > 0 else (b.l <= z.top and b.l >= z.bot - 0.5)
                if ft:
                    z.flip_done = True
                    fdir = -d
                    f_entry = z.bot if d > 0 else z.top
                    f_stop = z.top + 1 if d > 0 else z.bot - 1
                    tgt = self._opp_target(d, f_entry, self._k, fdir)
                    if tgt is None:
                        tgt = f_entry + fdir * abs(f_entry - f_stop) * 2
                    return self._enter("FLIP", fdir, f_entry, f_stop, tgt, z, b)
                if (b.c > z.top + 5) if d > 0 else (b.c < z.bot - 5):
                    z.flip_done = True   # ran away, no flip
        return []

    def _manage(self, b: Bar) -> list[Order]:
        t = self.trade
        assert t is not None
        d = t.dir
        t.bars_left -= 1

        def close(qty: int, tag: str, done: bool,
                  at: float | None = None) -> list[Order]:
            """`at` = the level that triggered this exit, so the fill is priced
            there instead of at this bar's close (see base.level_fill)."""
            qty = min(qty, t.remaining)
            if done:
                self.trade = None
            return [Order(self.symbol, -d, qty, tag=tag, reduce_only=True,
                          trigger_price=at)] if qty > 0 else []

        if not t.scalped:
            scalp_hit = (b.h >= t.scalp_px) if d > 0 else (b.l <= t.scalp_px)
            stop_hit = (b.l <= t.stop) if d > 0 else (b.h >= t.stop)
            if scalp_hit:                       # scalp wins intrabar tie
                px = level_fill(b, t.scalp_px, rising=d > 0)
                if not self.runner:             # take it all; no breakeven leg
                    return close(t.remaining, "scalp", done=True, at=px)
                t.scalped = True
                t.stop = t.entry                # runner to breakeven
                scalp_qty = t.size // 2
                t.remaining -= scalp_qty
                return close(scalp_qty, "scale", done=(t.remaining <= 0),
                             at=px) if scalp_qty > 0 else []
            if stop_hit:
                return close(t.remaining, "stop", done=True,
                             at=level_fill(b, t.stop, rising=d < 0))
        else:
            target_hit = (b.h >= t.target) if d > 0 else (b.l <= t.target)
            be_hit = (b.l <= t.stop) if d > 0 else (b.h >= t.stop)
            if target_hit:
                return close(t.remaining, "target", done=True,
                             at=level_fill(b, t.target, rising=d > 0))
            if be_hit:
                return close(t.remaining, "be", done=True,
                             at=level_fill(b, t.stop, rising=d < 0))
        if t.bars_left <= 0:                    # no level: genuinely at the market
            return close(t.remaining, "timeout", done=True)
        return []


__all__ = ["ZoneLifecycleStrategy"]
