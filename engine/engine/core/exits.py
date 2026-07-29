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
    ARM              MFE >= arm_mult x the SESSION'S OWN typical move. Scaled by
                     the market, so it means the same thing on a quiet ES day and
                     a wild NQ one. Arming on a multiple of the trade's own risk
                     instead was tested and FAILED out-of-sample -- it improved
                     totals while damaging the tail, i.e. the old mistake.
    PHASE 2 PROTECT  watch for the move ending and leave while still a good
                     winner.

    Reversal detectors are SELF-REFERENTIAL, so a 10-minute move and a 4-hour
    move are each judged against their own pace:
        decay  recent advance rate has fallen to `rev_f` of the rate that built
               the peak -- momentum death.
        range  price confined to a band under `rev_f` of what the trade
               achieved -- it has gone sideways into a range of no interest.
        retrace gives back `rev_f` of the run AFTER arming (never before).

    Measured, both samples, thresholds refitted to nothing:
        in-sample  29 live ES trades : total +$15,602, tail +$13,825, 8 armed
        out-of-sample 15 ESM5 sessions: total  +$8,188, tail    +$425, 65 armed
    Sign held on both measures across a different contract and year. The size of
    the tail benefit did NOT replicate, so treat the magnitude as unknown.
    """
    kind, tag = THESIS, "twophase"

    def __init__(self, arm_mult: float = 12.0, rev_kind: str = "decay",
                 rev_f: float = 0.1, win: int = 30, vol_win: int = 600) -> None:
        self.arm_mult, self.rev_kind, self.rev_f = arm_mult, rev_kind, rev_f
        self.win, self.vol_win = win, vol_win
        self._vol: list[float] = []
        self.reset()

    # the session's own scale, kept from the price stream the sleeve sees
    def note_price(self, px: float) -> None:
        self._vol.append(px)
        if len(self._vol) > self.vol_win:
            del self._vol[0]

    def unit(self) -> float:
        """Median absolute move over `win` samples -- the market's own ruler.
        Median, not std, so one spike cannot set the scale."""
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
        u = self._unit_at_entry or self.unit()
        if not self.armed:
            if u <= 0 or self._peak < self.arm_mult * u:
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
