"""Where is the structure? Every level source the engine already computes, in
one place, answering one question a strategy can act on.

WHY THIS EXISTS. trendjoin entered the instant confirmation arrived -- it bought
the move at its most extended point so far, which its own record calls expensive
(a median 7.5pt of heat per caught leg; six stops in one session on 2026-08-28
flipping direction each time). The obvious repair, "wait for a pullback of N
points", is a magic number: it says nothing about the market, it has to be
re-guessed per instrument (4pt on ES became 20pt on NQ purely to keep the ratio),
and a constant distance is exactly the kind of threshold this book has been
burned by before.

A pullback is not a DISTANCE. It is a retracement INTO SOMETHING -- a pivot, a
prior-day high, a gamma wall, the proximal edge of a demand zone, VWAP. Which
one is irrelevant and changes daily; what matters is that something is there.
So this asks the market where the structure is instead of asserting how far away
it should be, and if nothing is there the setup is declined rather than forced.

SOURCES, all already built and none of them new:
    pivots.py       floor pivots, prior-day H/L, round numbers
    zones.py        30m supply/demand zones (proximal edge)
    avwap.py        anchored VWAP and its bands
    gamma_levels.py prior-session put wall / call wall / zero-gamma flip

FAILS OPEN. A source that has not warmed up contributes nothing; with no levels
at all `pullback_to` returns None and every caller must read that as "no
opinion", never as "enter here".
"""
from __future__ import annotations

from ..core.events import Bar
from .avwap import AnchoredVWAP
from .bars import BarAggregator
from .pivots import SessionLevels
from .zones import ZoneBook, ZoneDetector


class LevelBook:
    def __init__(self, round_step: float = 50.0) -> None:
        self.piv = SessionLevels(round_step=round_step)
        # ZONES ARE A 30-MINUTE OBJECT. zones.py detects "30-minute
        # institutional supply/demand zones"; this book fed the detector RAW 1m
        # bars, so a 2-4 minute base-and-departure counted as structure and 19
        # sessions produced ~700 of them -- at which point every 2-point window
        # contains one and "wait for confluence" is satisfied 100% of the time,
        # which is as useless as the 1-in-108 it replaced. The painter and
        # zones_strategy both aggregate; this did not.
        self.agg = BarAggregator(("30m",))
        self.zdet = ZoneDetector()
        self.zbook = ZoneBook()
        self.vwap = AnchoredVWAP()
        self.gamma: dict | None = None          # injected per session; may be None
        self.extra: list = []                   # anything a caller wants counted
        self.last: float | None = None

    # ── accumulation ─────────────────────────────────────────────────────
    def on_bar(self, b: Bar) -> None:
        if b.tf != "1m":
            return
        self.last = b.c
        self.piv.update_bar(b)
        self.vwap.add(b.c, float(b.v))
        # detection and BREAKS happen on the 30m bar (a 30m close through a zone
        # is what invalidates it); TOUCHES tick with every 1m close, because a
        # zone is tested the moment price enters it. Without on_price nothing
        # ever left the virgin state, so the virgin filter in levels() filtered
        # nothing and the book kept every zone it had ever seen.
        for hb in self.agg.update(b):
            for z in self.zdet.update(hb):
                self.zbook.add(z)
            self.zbook.on_bar(hb)
        self.zbook.on_price(b.c, b.ts)

    def set_gamma(self, levels: dict | None) -> None:
        """Prior-session dealer-gamma walls, in FUTURE terms. Optional.

        MUST BE CALLED. The first version of this book shipped with the hook and
        nothing calling it, so 97% of entries fell back to floor pivots and the
        walls -- the source most worth waiting for -- were never in the book at
        all. tools/run_live.attach_level_books wires it now."""
        self.gamma = levels

    def set_extra(self, levels) -> None:
        """Additional (price, source) structure from outside: prior-day H/L,
        overnight extremes, anything the caller already knows."""
        self.extra = list(levels or [])

    # ── the question ─────────────────────────────────────────────────────
    def levels(self) -> list[tuple[float, str]]:
        """Every level currently known, as (price, source). Deduplicated only by
        source, never merged: two sources landing on the same price is exactly
        the confluence a caller may want to see."""
        out: list[tuple[float, str]] = []
        out += [(float(x), "pivot") for x in self.piv.pivots]
        for z in getattr(self.zbook, "zones", []) or []:
            # `virgin` is a PROPERTY. This read it as z.virgin(), which calls a
            # bool and raises TypeError -- and the except below was broad enough
            # to swallow it, so no zone EVER reached this list and the book held
            # pivots + VWAP + gamma only. That is the second time this file has
            # shipped a level source that is wired and never fed (see set_gamma),
            # and it invalidates the confluence measurement trend_join records
            # against itself: agreement cannot involve an absent source.
            #
            # The except is now narrow. It exists for a zone still mid-build
            # whose proximal() has nothing to return; a TypeError from calling a
            # non-callable is a coding mistake and must not be hidden again.
            if not z.virgin:
                continue
            try:
                out.append((float(z.proximal()), "zone"))
            except (ValueError, AttributeError, IndexError):   # zone mid-build
                continue
        v = self.vwap.value      # property, not a call
        if v and v == v:
            out.append((float(v), "vwap"))
        if self.gamma:
            for k in ("put_wall", "call_wall", "flip"):
                x = self.gamma.get(k)
                if x is not None and x == x:
                    out.append((float(x), k))
        out += [(float(x), str(s)) for x, s in self.extra]
        return sorted(out)

    def clusters(self, tol: float) -> list[tuple[float, str, int]]:
        """Group levels within `tol` of each other into ONE structure.

        A price where a call wall, a prior-day high and a zone edge coincide is
        not three levels, it is one strong level, and the score is how many
        DISTINCT sources agree there. Two pivots at the same price count once:
        agreement between different kinds of evidence is the signal, repetition
        within a kind is not."""
        lv = self.levels()
        if not lv:
            return []
        out, cur = [], [lv[0]]
        for x, s in lv[1:]:
            if x - cur[0][0] <= tol:
                cur.append((x, s))
            else:
                out.append(cur)
                cur = [(x, s)]
        out.append(cur)
        res = []
        for g in out:
            srcs = sorted({s for _, s in g})
            px = sum(x for x, _ in g) / len(g)
            res.append((px, "+".join(srcs), len(srcs)))
        return res

    def pullback_to(self, price: float, trade_dir: int,
                    max_dist: float | None = None, tol: float = 0.0,
                    min_dist: float = 0.0):
        """The structure a retracement would run into: (price, sources, score).

        THE STRONGEST within reach, not the nearest. That distinction is the
        whole correction -- returning the nearest turned this into "enter at the
        closest floor pivot", which is a shallow constant wearing a costume and
        measured worse than the fixed distance it replaced.

        Ties on score break toward the NEARER cluster: given equal evidence,
        less give-back. `tol` groups sources into one structure and must be
        passed as a fraction of a typical session's range by the caller, never
        as a point count."""
        cands = []
        for x, src, n in (self.clusters(tol) if tol > 0 else
                          [(a, b, 1) for a, b in self.levels()]):
            d = (price - x) if trade_dir > 0 else (x - price)
            if d <= 0.0 or d < min_dist:
                continue
            if max_dist is not None and d > max_dist:
                continue
            cands.append((-n, d, x, src))
        if not cands:
            return None
        _, _, x, src = min(cands)          # strongest, then nearest
        return (x, src, -min(cands)[0])


__all__ = ["LevelBook"]
