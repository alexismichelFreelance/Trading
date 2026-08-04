"""Composable EXIT STRATEGIES.

Every sleeve in this engine had its entry researched and its exit chosen by
feel — a stop, a clock, or an MOC. Those are not exit strategies. A stop is what
you do when you were wrong; a clock is an admission you have no exit thesis at
all. Neither answers "why am I still in this trade?".

2026-07-28 made the cost concrete: NQ:vwapbreak ran +127.75pt in its favour and
closed at -14.50 on the MOC after 278 minutes. NQ:opendrive held a loser for 87
minutes to -346 while NQ:onbreak cut the SAME entry at -118 — the exit rule
alone was worth 228 points on an identical trade.

So exits become first-class and COMBINATORIAL: a sleeve is (entry logic) x (an
ordered list of exit rules). One entry can therefore be paper-traded under many
exit strategies at once, and the record decides which survives — instead of one
hand-picked exit per sleeve, never measured.

Rules are ordered by KIND, deliberately:

    THESIS      the predicted move happened            -> we were right
    INVALIDATION the reason for the trade is gone       -> we know sooner than a stop
    PEER        other sleeves say the trade is over     -> information no sleeve has alone
    INSURANCE   stop / time / MOC                       -> only what is left over

First rule to fire wins, so a target or an invalidation always pre-empts a stop.

The PEER rules are the ones that need the whole roster: a sleeve cannot see that
three others just took the other side, or that the ones agreeing with it have
all gone flat. That is what the Signal channel carries.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# rule kinds, in the order they should be consulted
THESIS, INVALIDATION, PEER, INSURANCE = "thesis", "invalidation", "peer", "insurance"


@dataclass
class ExitCtx:
    """Everything an exit rule may look at. Sleeves fill in what they can; a
    rule that needs a field the sleeve never sets simply never fires, so a
    sleeve can adopt rules piecemeal without special-casing."""
    ts: int
    price: float
    dir: int                       # +1 long, -1 short
    entry_px: float
    entry_ts: int
    peak_fe: float = 0.0           # best favourable excursion so far, points
    target: float | None = None    # sleeve-supplied THESIS level
    invalidated: bool = False      # sleeve-supplied INVALIDATION flag
    minute_et: int = 0
    # peer state, maintained by the sleeve from Signal events
    peers_against: int = 0         # peers currently opposing our direction
    peers_with: int = 0            # peers currently agreeing
    peers_with_peak: int = 0       # most that ever agreed while we held
    consensus: float = 0.0         # net agreement, -1..+1, aligned to OUR dir


class ExitRule:
    kind = INSURANCE
    tag = "exit"

    def check(self, c: ExitCtx) -> str | None:
        raise NotImplementedError


# ── THESIS ───────────────────────────────────────────────────────────────────
class TargetExit(ExitRule):
    """The move we predicted happened. The level comes from the SIGNAL (e.g. the
    span a sweep just covered, the next pivot, the VWAP), never from a tick count
    picked by hand — so it scales with the setup instead of with my guess."""
    kind, tag = THESIS, "target"

    def check(self, c):
        if c.target is None:
            return None
        return self.tag if (c.price - c.target) * c.dir >= 0 else None


class PartialGiveBackExit(ExitRule):
    """We were right, and are now handing it back. Exits once a fraction of the
    best excursion has been surrendered. Unlike a trailing stop this is scaled by
    what the trade ACHIEVED, not by a fixed distance — NQ:vwapbreak gave back
    127.75pt and a fixed trail never noticed."""
    kind, tag = THESIS, "giveback"

    def __init__(self, frac: float = 0.5, min_fe: float = 2.0):
        self.frac, self.min_fe = frac, min_fe

    def check(self, c):
        if c.peak_fe < self.min_fe:
            return None
        fe = (c.price - c.entry_px) * c.dir
        return self.tag if fe <= c.peak_fe * (1.0 - self.frac) else None


# ── INVALIDATION ─────────────────────────────────────────────────────────────
class InvalidationExit(ExitRule):
    """The condition that justified the entry is no longer true. The sleeve
    decides what that means and sets the flag; leaving here is strictly better
    than waiting for a stop to discover the same thing later and worse."""
    kind, tag = INVALIDATION, "invalid"

    def check(self, c):
        return self.tag if c.invalidated else None


# ── PEER (needs the roster) ──────────────────────────────────────────────────
class PeerOpposeExit(ExitRule):
    """`k` peers have OPENED against us. One sleeve disagreeing is noise; several
    committing the other way is evidence this sleeve cannot see on its own."""
    kind, tag = PEER, "peer-oppose"

    def __init__(self, k: int = 1):
        self.k = k

    def check(self, c):
        return f"{self.tag}{self.k}" if c.peers_against >= self.k else None


class SupportLostExit(ExitRule):
    """The peers that AGREED with us have gone. Nobody took the other side — our
    confirmation simply evaporated, which is the 'they stopped agreeing' signal
    and is invisible to any single sleeve."""
    kind, tag = PEER, "support-lost"

    def __init__(self, frac: float = 0.5, min_peak: int = 2):
        self.frac, self.min_peak = frac, min_peak

    def check(self, c):
        if c.peers_with_peak < self.min_peak:
            return None
        return self.tag if c.peers_with <= c.peers_with_peak * (1.0 - self.frac) else None


class ConsensusFlipExit(ExitRule):
    """Net agreement across the roster has turned against our direction."""
    kind, tag = PEER, "consensus-flip"

    def __init__(self, level: float = 0.0):
        self.level = level

    def check(self, c):
        return self.tag if c.consensus <= self.level else None


# ── INSURANCE (never a decision, only a floor) ───────────────────────────────
class StopExit(ExitRule):
    kind, tag = INSURANCE, "stop"

    def __init__(self, points: float):
        self.points = points

    def check(self, c):
        return self.tag if (c.price - c.entry_px) * c.dir <= -self.points else None


class TimeExit(ExitRule):
    kind, tag = INSURANCE, "timeout"

    def __init__(self, seconds: float):
        self.ns = int(seconds * 1e9)

    def check(self, c):
        return self.tag if c.ts - c.entry_ts >= self.ns else None


class SessionEndExit(ExitRule):
    kind, tag = INSURANCE, "eod"

    def __init__(self, minute_et: int = 15 * 60 + 59):
        self.minute_et = minute_et

    def check(self, c):
        return self.tag if c.minute_et >= self.minute_et else None


@dataclass
class ExitPolicy:
    """An ordered list of rules, consulted THESIS -> INVALIDATION -> PEER ->
    INSURANCE regardless of the order they were supplied in, so that a stop can
    never pre-empt a target that fired on the same tick."""
    rules: list[ExitRule] = field(default_factory=list)
    name: str = ""

    _ORDER = {THESIS: 0, INVALIDATION: 1, PEER: 2, INSURANCE: 3}

    def __post_init__(self):
        self.rules = sorted(self.rules, key=lambda r: self._ORDER[r.kind])

    def check(self, c: ExitCtx) -> str | None:
        for r in self.rules:
            hit = r.check(c)
            if hit:
                return hit
        return None


__all__ = [
    "ExitCtx", "ExitPolicy", "ExitRule",
    "TargetExit", "PartialGiveBackExit", "InvalidationExit",
    "PeerOpposeExit", "SupportLostExit", "ConsensusFlipExit",
    "StopExit", "TimeExit", "SessionEndExit",
    "THESIS", "INVALIDATION", "PEER", "INSURANCE",
]


# ── two-phase: ride, then protect ────────────────────────────────────────────
class TwoPhaseExit(ExitRule):
    """Let winners run, then hunt the reversal. The only exit here that
    survived an out-of-sample test.

    PHASE 1 RIDE     no exit at all beyond the sleeve's disaster stop. A move is
                     never interrupted on its way up -- no clock, no take-profit,
                     no give-back. This is what preserves the tail: 5 of 41
                     trades were 63% of all available P&L, and every uniform
                     rule tested destroyed exactly those.
    ARM              MFE >= arm_mult x a trailing typical move (see `unit`).
                     Arming on a multiple of the trade's own risk instead was
                     tested and FAILED out-of-sample -- it improved totals while
                     damaging the tail, i.e. the old mistake.
                     Do NOT read this as "the session's typical move". The ruler
                     is built from the trailing `vol_win` minutes, which at the
                     open is ~10 hours and therefore almost entirely OVERNIGHT.
                     Measured on 30 ES sessions, the overnight typical move runs
                     1.6-2.8x smaller than the RTH one, and NO estimator
                     available at 09:30 predicted the RTH value (R^2 ~ 0.01 for
                     overnight range, overnight typical move, and this trailing
                     window alike; a lookahead estimator gets 0.42-0.60). So the
                     ruler sets a level, it does not forecast the day.
    PHASE 2 PROTECT  watch for the move ending and leave while still a good
                     winner.

    Reversal detectors are SELF-REFERENTIAL, so a 10-minute move and a 4-hour
    move are each judged against their own pace:
        decay  recent advance rate has fallen to `rev_f` of the rate that built
               the peak -- momentum death.
        range  price confined to a band under `rev_f` of what the trade
               achieved -- it has gone sideways into a range of no interest.
        retrace gives back `rev_f` of the run AFTER arming (never before).

    EVIDENCE -- READ THIS BEFORE TRUSTING arm_mult=12.
    The earlier results (in-sample 29 live ES trades +$15,602; out-of-sample 15
    ESM5 sessions +$8,188) were measured with a ruler this class no longer uses:
    it was computed over each session's WHOLE set of bars, including bars after
    the trade. That is lookahead, and it is favourable lookahead -- on a day that
    turned out volatile the unit is larger, so arming happens later and the rule
    rides longer, exactly on the big-move days that carry the result. Those two
    numbers do not describe this code and are NOT evidence for it.

    What HAS been re-measured causally (tools/exit_causal_sweep.py, ~165 replay
    round-trips each on ESH5 and ESM5, both rulers, arming distance swept 0.5 to
    1200 points):

      1. The adaptive ruler never beats a fixed distance. Matched on how many
         trades arm, `arm_pts` won every ESH5 cell (+$50.5k vs +$43.2k at ~66
         armed; +$56.0k vs +$28.3k at ~50) and tied on ESM5. That is what an
         estimator with R^2 ~ 0.01 is worth: the day-to-day wobble in the ruler
         is noise, and noise in a threshold is worse than a constant.
      2. In POINTS the two contracts agree: both peak at 24-32 ES points of
         arming distance, improving total AND tail, and fall away on both sides.
         Read in multiplier space that same curve looked like an unbounded ramp,
         because the two rulers differ ~19x in scale -- 12x meant 3 points in a
         tick-fed sleeve and 57 in a bar-fed one, on opposite sides of the peak.

    STILL NOT VALIDATED. Two contracts, 15 sessions each, and the 24-32pt band
    was read off these same curves, so it is in-sample. It is also ES points and
    will not port to NQ unscaled. The roster therefore carries several arming
    distances at once and lets the forward record choose, rather than baking in
    a number fitted to 30 sessions.
    """
    kind, tag = THESIS, "twophase"

    def __init__(self, arm_mult: float = 12.0, rev_kind: str = "decay",
                 rev_f: float = 0.1, win_min: int = 30, vol_win_min: int = 600,
                 arm_pts: float | None = None) -> None:
        """`arm_pts` arms at a FIXED distance in points and ignores the ruler
        entirely. Measured better than the ruler at matched arm rate (see
        EVIDENCE), and it is defined from the first tick of a session -- the
        ruler is not, so a ruler-based sleeve simply cannot arm early in a day.
        Set one or the other; `arm_pts` wins when both are given."""
        self.arm_mult, self.rev_kind, self.rev_f = arm_mult, rev_kind, rev_f
        self.arm_pts = arm_pts
        self.win, self.vol_win = win_min, vol_win_min
        self._vol: list[float] = []          # one close per MINUTE, not per call
        self._last_min = -1
        self.reset()

    # The market's own scale, on a fixed one-minute grid.
    #
    # `ts` is REQUIRED. It used to be a bare price appended once per call, so
    # `vol_win=600` meant 600 *samples* -- which is 600 minutes (10 hours) in a
    # sleeve that feeds it from on_bar, and about thirty SECONDS in one that
    # feeds it from on_trade. Same parameter, 50x apart, and the trade-fed
    # sleeve armed after 3 points instead of 42, which deletes the ride phase
    # that the whole rule exists to protect. Bucketing by minute makes `win` and
    # `vol_win` mean minutes no matter which callback a sleeve wires up, and
    # making `ts` mandatory means a sleeve that forgets fails loudly at the call
    # instead of silently running a different rule.
    def note_price(self, px: float, ts: int) -> None:
        m = int(ts) // 60_000_000_000
        if m == self._last_min and self._vol:
            self._vol[-1] = px               # same minute -> running close
            return
        self._last_min = m
        self._vol.append(px)
        if len(self._vol) > self.vol_win:
            del self._vol[0]

    def unit(self) -> float:
        """Median absolute move over `win` MINUTES -- the market's own ruler.
        Median, not std, so one spike cannot set the scale. Deliberately spans
        the overnight: at 09:30 a 600-minute window is ~10 hours and is
        therefore an overnight estimate. Measured on 30 ES sessions, the
        overnight typical move runs 1.6-2.8x SMALLER than the RTH one, and no
        causal estimator predicted the RTH value (R^2 ~ 0.01), so this is a
        level, not a forecast -- do not read `arm_mult` as adaptive."""
        n = len(self._vol)
        if n <= self.win:
            return 0.0
        a = self._vol
        d = sorted(abs(a[i] - a[i - self.win]) for i in range(self.win, n))
        return d[len(d) // 2]

    def reset(self) -> None:
        self.armed = False
        self._adv: list[float] = []
        self._peak = -1e18
        self._peak_i = 0
        self._unit_at_entry = 0.0

    def start(self, direction: int, entry_px: float) -> None:
        self.reset()
        self._dir, self._entry = direction, entry_px
        self._unit_at_entry = self.unit()

    def check(self, c: ExitCtx) -> str | None:
        adv = (c.price - c.entry_px) * c.dir
        self._adv.append(adv)
        i = len(self._adv) - 1
        if adv > self._peak:
            self._peak, self._peak_i = adv, i
        if not self.armed:
            if self.arm_pts is not None:
                need = self.arm_pts
            else:
                u = self._unit_at_entry or self.unit()
                if u <= 0:
                    return None                  # no ruler yet -> cannot arm
                need = self.arm_mult * u
            if self._peak < need:
                return None                      # PHASE 1: ride, untouched
            self.armed = True
            return None
        k = max(1, min(self.win, i))
        if self.rev_kind == "decay":
            rate = (self._adv[i] - self._adv[i - k]) / k
            j = self._peak_i
            best = max((self._adv[j] - self._adv[max(0, j - k)]) / k, 1e-9)
            if rate <= self.rev_f * best:
                return f"{self.tag}-decay"
        elif self.rev_kind == "range":
            seg = self._adv[max(0, i - k):i + 1]
            if seg and (max(seg) - min(seg)) <= self.rev_f * max(self._peak, 1e-9):
                return f"{self.tag}-range"
        elif self.rev_kind == "retrace":
            if adv <= self._peak - self.rev_f * max(self._peak, 1e-9):
                return f"{self.tag}-retrace"
        return None


__all__ += ["TwoPhaseExit"]
