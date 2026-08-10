"""A QUALIFIED break, per the user's specification (2026-08-08).

    1. price breaks the OPENING RANGE
    2. the move extends at least one full RANGE WIDTH past the break
    3. no close back through VWAP on the way
    then: enter on the pullback to VWAP.
    and: 3 consecutive 5-minute closes on the other side of VWAP = regime
         change, the other side is in control.

Only step 4 exists today. The current sleeve breaks a VWAP +/- 1*sigma BAND (not
the opening range), proves continuation with "a new session extreme close" (not
an extension of one range width), has NO cleanliness condition at all, and flips
on a single close through VWAP by a quarter of the band width rather than three
5m closes.

Points 2 and 3 are the ones doing real work in the spec -- they are what
separates a break that is going somewhere from a poke -- and neither has any
equivalent in the code.

Choices made where the spec is silent, stated rather than hidden:
  * opening range = 09:30-10:00 ET, the same window opendrive measures.
  * "no close back through VWAP" is tested on the sleeve's own 1m closes (the
    strictest reading). The 5m rule is used only where the spec says 5m.
  * "at the pullback at VWAP (or close to it)" is a resting limit AT the line;
    on bar data it fills when a bar's range contains it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.events import Bar  # noqa: E402
from engine.strategies.vwap_break import VwapBreakStrategy  # noqa: E402

NS = 60_000_000_000


def _bars(day, specs):
    """specs: (hh:mm, o, h, l, c) -> 1m bars with volume, in ET."""
    out = []
    for hhmm, o, h, l, c in specs:
        ts = int(pd.Timestamp(f"{day} {hhmm}", tz="America/New_York").value)
        out.append(Bar(ts, "1m", o, h, l, c, 1000, "ES"))
    return out


def _run(bars, **kw):
    s = VwapBreakStrategy("ES", entry_mode="qualified", **kw)
    orders = []
    for b in bars:
        orders += s.on_bar(b) or []
    return s, orders


def _session(day, opening, after):
    """30 flat opening-range minutes, then `after` = list of (hh:mm,o,h,l,c)."""
    specs = []
    lo, hi = opening
    for m in range(30):
        px = lo + (hi - lo) * (m % 2)
        specs.append((f"09:{30 + m:02d}", px, hi, lo, px))
    return _bars(day, specs + after)


# ── the qualifying conditions ────────────────────────────────────────────────

def test_break_without_the_full_range_extension_does_not_qualify():
    """Condition 2. Range is 10 pts, so an up-break must reach OR_high + 10.
    Stopping short of that is a poke, not a qualified break."""
    after = [(f"10:{m:02d}", 7010.0, 7015.0, 7009.0, 7014.0) for m in range(30)]
    s, _ = _run(_session("2026-08-03", (7000.0, 7010.0), after))
    assert s.n_qualified == 0, "a 5pt extension on a 10pt range qualified"


def test_full_extension_qualifies():
    after = [(f"10:{m:02d}", 7010.0 + m, 7012.0 + m, 7009.0 + m, 7011.0 + m)
             for m in range(30)]
    s, _ = _run(_session("2026-08-03", (7000.0, 7010.0), after))
    # _qualified is CONSUMED into _armed on the same bar, so count it instead
    assert s.n_qualified == 1, f"a full-range extension did not qualify ({s.n_broke} breaks)"


def test_a_close_back_through_vwap_disqualifies_the_break():
    """Condition 3. The extension is reached, but price closed back through VWAP
    on the way -- the move was not clean, so it never qualifies."""
    # break, then a close back through VWAP before the extension is reached.
    # The recovery afterwards deliberately stays BELOW the opening-range high, so
    # no fresh break can qualify and the assertion is about the disqualified one.
    after = ([(f"10:{m:02d}", 7011.0, 7013.0, 7010.0, 7012.0) for m in range(5)]
             # closes back through VWAP (~7005) but stays INSIDE the opening
             # range, so it disqualifies the up-break without itself being a
             # qualified down-break (a bar that closes below the range low AND
             # reaches a full width below IS one, correctly)
             + [("10:05", 7011.0, 7011.0, 7001.0, 7002.0)]
             + [(f"10:{m:02d}", 7000.0, 7006.0, 6996.0, 7002.0) for m in range(6, 40)])
    s, _ = _run(_session("2026-08-03", (7000.0, 7010.0), after))
    assert s.n_qualified == 0, "a break that closed back through VWAP qualified"
    assert s.n_disqualified >= 1, "the VWAP cross was not recorded as a disqualification"


def test_entry_is_a_resting_limit_at_the_line_not_a_chase():
    """Condition 4. After qualifying, the order must be a LIMIT at VWAP."""
    from engine.core.orders import OrderType
    up = [(f"10:{m:02d}", 7010.0 + 2 * m, 7012.0 + 2 * m, 7009.0 + 2 * m, 7011.0 + 2 * m)
          for m in range(20)]
    # The rally drags VWAP UP with it, so the pullback has to come back to where
    # the line now IS (~7015), not to where it was when the break happened.
    back = []
    for m in range(20, 45):
        px = 7049.0 - 5.0 * (m - 20)
        back.append((f"10:{m:02d}", px, px + 2, px - 2, px))
    s, orders = _run(_session("2026-08-03", (7000.0, 7010.0), up + back))
    ent = [o for o in orders if "entry" in (o.tag or "")]
    assert ent, (f"qualified={s.n_qualified} armed={s._armed is not None}; "
                 f"no entry on the pullback")
    assert ent[0].type == OrderType.LIMIT, f"chased with {ent[0].type}"


# ── the regime rule ──────────────────────────────────────────────────────────

def test_three_consecutive_5m_closes_the_other_side_flip_the_regime():
    s = VwapBreakStrategy("ES", entry_mode="qualified")
    for _ in range(2):
        s._note_5m_close(below=True)
    assert s._regime != -1, "flipped on two closes"
    s._note_5m_close(below=True)
    assert s._regime == -1, "three consecutive closes below did not flip the regime"


def test_a_close_back_on_the_original_side_resets_the_count():
    """CONSECUTIVE means consecutive."""
    s = VwapBreakStrategy("ES", entry_mode="qualified")
    s._note_5m_close(below=True)
    s._note_5m_close(below=True)
    s._note_5m_close(below=False)          # back above -> count dies
    s._note_5m_close(below=True)
    s._note_5m_close(below=True)
    assert s._regime != -1, "a broken run still flipped the regime"


def test_the_regime_flip_exits_a_position_held_against_it():
    s = VwapBreakStrategy("ES", entry_mode="qualified")
    s.pos = 1
    s.trade = {"side": 1, "entry": 7000.0, "stop": 10.0, "trail": 15.0,
               "peak": 0.0, "band": 8.0}
    out = []
    for _ in range(3):
        out += s._apply_regime(below=True, price=6990.0) or []
    assert out, "regime flipped against a long and nothing closed it"
    assert out[0].side == -1 and out[0].reduce_only


def test_defaults_are_untouched():
    """market/retest keep working; qualified is opt-in."""
    assert VwapBreakStrategy("ES").entry_mode == "market"
    assert VwapBreakStrategy("ES", entry_mode="retest").entry_mode == "retest"
