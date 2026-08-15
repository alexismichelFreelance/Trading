"""BaseStrategy — no-op defaults so concrete strategies override only what they
use. Satisfies the core.ports.Strategy protocol structurally."""
from __future__ import annotations

from ..core.events import (Bar, BookFlow, DepthUpdate, Fill, PositionUpdate,
                           Quote, Signal, Trade)
from ..core.exits import ExitCtx, ExitPolicy
from ..core.orders import Order
from ..core.timeutil import et_minute_of_day, et_session_date


SESSION_FLAT_MIN = 15 * 60 + 59      # 15:59 ET — last minute an intraday sleeve holds
SESSION_OPEN_MIN = 18 * 60           # 18:00 ET — Globex opens; a NEW session begins


def level_fill(bar: Bar, level: float, rising: bool) -> float:
    """The honest fill price for an exit triggered by the tape reaching `level`.

    `rising` — the level sits ABOVE and was reached by price going up (a short's
    stop, a long's target). False for the mirror.

    Every sleeve detects its stop and target on the BAR (`bar.l <= stop`,
    `bar.h >= target`) and then sends a market order, which fills at the engine's
    last price — that bar's CLOSE. So a stop tripped by a wick that closed back
    on the good side books BETTER than the stop, and a target touched and given
    back books WORSE than the target. The first case is the common shape of a
    stop run, so the net bias flatters. Live on 2026-08-12 ES:pivot booked +4.00
    points on a stopped-out long.

    The level is the fill. The exception is a GAP: if the bar OPENED already
    beyond the level there was never a print at it, so the open is the fill and
    the gap is slippage that really would have been paid."""
    beyond = bar.o >= level if rising else bar.o <= level
    return float(bar.o if beyond else level)


class BaseStrategy:
    symbol: str = ""
    gamma = None          # optional GammaRegime — a STRATEGY choice, not an engine gate
    # Optional GammaCurve (engine/features/gamma_curve.py). None = no filter.
    pocket = None
    pocket_min_edge = 0.0   # points from a pocket edge; 0 disables that filter
    # The last price this sleeve was shown. The local-sign gate needs a price and
    # BookFlow carries none, so flow's gate -- which fires on a book event --
    # would silently fall back to the percentile and make its twin a copy.
    last_seen_px: float | None = None
    # Per-VARIANT override of the regime a sleeve asks for. None =
    # the call site decides (its natural want). Set on a twin to test
    # the opposite classification without touching the sleeve.
    regime_want: str | None = None

    # OPT-OUT, deliberately. A sleeve that says nothing is flattened at the
    # session boundary; only a genuine swing sleeve sets this True. The audit on
    # 2026-08-01 found zones_strategy (the RISK-SIZED one, 5+ contracts) and
    # ignition with no end-of-day flat at all -- zones' only bound was K_BARS=16
    # bars, so a late entry rode through the close, through the 17:00 ET Globex
    # halt and into the next session. Making the default safe means forgetting
    # costs you a flat position, not an unplanned overnight one.
    holds_overnight: bool = False

    @staticmethod
    def session_over(ts: int) -> bool:
        """True inside the CLOSING WINDOW: 15:59 ET up to the 18:00 ET Globex
        reopen.

        A bounded window, not `>= 15:59`. The unbounded form flattens at 20:00 ET
        too -- but by then et_session_date has already rolled to the next day and
        an 18:00+ position belongs to a NEW session, so an overnight entry was
        being force-closed the moment it was opened.

        Always ET, never a UTC hour: a fixed UTC hour is a different ET hour
        either side of a DST change (21 UTC is 17:00 ET in July, 16:00 ET in
        December), so a UTC gate silently moves twice a year."""
        return SESSION_FLAT_MIN <= et_minute_of_day(ts) < SESSION_OPEN_MIN

    def gamma_entry_ok(self, ts: int, want: str, price: float | None = None) -> bool:
        """Dealer-gamma ENTRY filter, opt-in per strategy.

        TWO SOURCES, and they disagree almost always.

        `self.gamma` (GammaRegime) reads `gexp` -- a 252-day percentile of the
        AGGREGATE book from SqueezeMetrics. It answers "is today's total gamma
        large relative to the past year". Measured against the regime where
        price actually sat, over 25 sessions with both available, it agreed
        7/25 (28%) -- it printed LONG on 18 of 25 while price was in a
        short-gamma pocket on 23 of 25.

        `self.pocket` (GammaCurve) reads the sign of cumulative gamma AT PRICE.
        That is the quantity the hedging argument is actually about: below the
        flip dealers hedge with the move (amplify), above it against (pin).

        When a curve AND a price are available the local sign wins, because it
        is the thing the mechanism describes. Otherwise this falls back to the
        percentile, and to True if neither is present -- an absent regime must
        never stop a sleeve trading.

        want='short': continuation sleeves, which need moves to compound.
        want='long':  mean-reversion sleeves. Exits are never filtered."""
        want = self.regime_want or want
        px = price if price is not None else self.last_seen_px
        if self.pocket is not None and px is not None:
            a = self.pocket.at(px)
            if a is not None and a.get("local_sign", 0) != 0:
                sg = a["local_sign"] < 0
                return sg if want == "short" else not sg
        if self.gamma is None:
            return True
        sg = self.gamma.is_short_gamma(et_session_date(ts))
        if sg is None:
            return True
        return sg if want == "short" else not sg


    def pocket_entry_ok(self, price: float) -> bool:
        """Stand down for a CONTINUATION entry taken near a gamma pocket edge.

        Measured per bar over 30 ES / 20 NQ sessions
        (strategy_lab/gamma_pocket_behaviour.py), variance ratio at 30 bars:

            deep in a SHORT pocket   1.25 ES / 1.34 NQ   moves compound
            near the pocket EDGE     0.86 ES / 0.73 NQ   moves revert
            deep in a LONG pocket    0.84 ES / 0.50 NQ   moves revert

        and the same split on the trades the sleeves already made
        (strategy_lab/gamma_gate_check.py):

            trend sleeves, SHORT, deep        442 trips  +50,722
            trend sleeves, SHORT, near edge   105 trips  -15,212

        Near the boundary price stops trending in BOTH regimes, so a
        continuation entry there is taken into mean reversion. This filters that
        one case and nothing else.

        Fail-open: no curve, no threshold, or a book with no crossing at all (8
        of 28 SPX sessions) allows the entry. Exits are never filtered."""
        if self.pocket is None or self.pocket_min_edge <= 0.0:
            return True
        a = self.pocket.at(price)
        if a is None:
            return True
        d = a.get("dist_to_flip")
        return d is None or abs(d) >= self.pocket_min_edge

    def on_trade(self, e: Trade) -> list[Order]:
        return []

    def on_quote(self, e: Quote) -> list[Order]:
        return []

    def on_depth(self, e: DepthUpdate) -> list[Order]:
        return []

    def on_bar(self, e: Bar) -> list[Order]:
        return []

    def on_bookflow(self, e: BookFlow) -> list[Order]:
        return []

    # ── peer state, maintained from the Signal channel ────────────────────
    # Any sleeve gets peers_with / peers_against for free by calling
    # note_peer() from on_signal. It is INTENT only -- a peer's position state
    # never crosses, so this cannot reintroduce the 2026-07-09 cross-sleeve
    # duplicate-flatten bug that owner-only fill attribution fixed.
    def _peer_reset(self) -> None:
        self._peer_dir: dict[str, int] = {}
        self._peers_with_peak = 0

    def note_peer(self, e: "Signal", my_dir: int) -> None:
        """Record a peer's current directional intent. A reduce_only signal
        means that peer is LEAVING, so it stops counting either way."""
        if not hasattr(self, "_peer_dir"):
            self._peer_reset()
        if e.reduce_only:
            self._peer_dir.pop(e.source, None)
        else:
            self._peer_dir[e.source] = e.side
        if my_dir:
            self._peers_with_peak = max(self._peers_with_peak,
                                        self.peers_with(my_dir))

    def peers_with(self, my_dir: int) -> int:
        return sum(1 for d in getattr(self, "_peer_dir", {}).values() if d == my_dir)

    def peers_against(self, my_dir: int) -> int:
        return sum(1 for d in getattr(self, "_peer_dir", {}).values() if d == -my_dir)

    def peer_consensus(self, my_dir: int) -> float:
        """Net agreement aligned to OUR direction, -1..+1. 0 when nobody is in."""
        d = getattr(self, "_peer_dir", {})
        if not d:
            return 0.0
        return sum(1 if v == my_dir else -1 for v in d.values()) / len(d)

    def exit_ctx(self, ts: int, price: float, my_dir: int, entry_px: float,
                 entry_ts: int, minute_et: int = 0, target=None,
                 invalidated: bool = False, peak_fe: float = 0.0) -> ExitCtx:
        """Assemble the context an ExitPolicy needs, peer fields included."""
        return ExitCtx(ts=ts, price=price, dir=my_dir, entry_px=entry_px,
                       entry_ts=entry_ts, peak_fe=peak_fe, target=target,
                       invalidated=invalidated, minute_et=minute_et,
                       peers_against=self.peers_against(my_dir),
                       peers_with=self.peers_with(my_dir),
                       peers_with_peak=getattr(self, "_peers_with_peak", 0),
                       consensus=self.peer_consensus(my_dir))

    def on_signal(self, e: Signal) -> list[Order]:
        """Another strategy just signalled. ADVISORY: this carries intent, never
        position state -- peers can never mutate this sleeve's book. Override to
        exit (or stand down) when a peer signals against you; the default
        ignores peers entirely, so every existing sleeve is unaffected and
        replay parity is untouched."""
        return []

    def on_fill(self, e: Fill) -> None:
        return None

    def on_position(self, e: PositionUpdate) -> None:
        return None

    def reset_for_live(self) -> None:
        """Called ONCE at the warmup->live flip. Warmup dispatches bars so
        feature/zone detectors warm up, but the warmup gate SUPPRESSES the
        resulting orders — leaving any 'I've acted' trade-lifecycle state
        (self.trade, self.entered, fade_done, position) corrupted by trades that
        never actually filled. Override to reset that trade state to flat/fresh
        while KEEPING warm detection (zones, averages, HMM). No-op by default;
        never called in replay, so parity is unaffected."""
        return None

    def restore_state(self, pos: int, avg_px: float) -> bool:
        """Resume managing an ALREADY-OPEN position after an engine restart
        (rebuilt from claude_paper_fills). Called at the warmup->live flip,
        AFTER reset_for_live. Return True only if this sleeve can correctly
        manage the position from just (pos, avg_px) — the engine then reseeds
        its attributed book so exits/reduce_only work. Default False: the sleeve
        needs richer entry state (stop/target/scalp) that (pos, avg_px) can't
        supply, so the open position is left flat and logged rather than held
        unmanaged. Only overnight-holding, price-stateless sleeves (IBS) opt in.
        Never called in replay."""
        return False


__all__ = ["BaseStrategy"]
