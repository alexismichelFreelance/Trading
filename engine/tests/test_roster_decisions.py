"""Roster decisions taken on 2026-08-08 evidence, pinned so they cannot drift back.

These change WHICH configuration each label builds. The strategy classes keep
their original defaults so the parity suites (tests/parity/) stay valid -- what
is deployed is a roster question, not a change to a validated contract.

1. ZONES: no BREAK setup, no breakeven RUNNER.
   Across 36 ES sessions, split by the setup that opened the trade:
       flip   6 trips  +7,288  100% win
       fade   7 trips  +6,412   86% win
       break  4 trips  -1,938   25% win
   and the "scale" rows -- which are the RUNNER left after half comes off at
   +4 -- were 4 trips, -2,100, 25% win. So the break setup loses, and holding a
   runner to breakeven gives back more than it makes. Take the whole position at
   the scalp instead.

2. VWAPBREAK: entry at the line, not chasing the break.
   On the corrected (VWAPX, midnight-anchored) line over 19 sessions:
       vwapbreak_retest  +3,425  +201/day
       vwapbreak         -3,750  -221/day
   Same exits, same everything; only the entry differs.

3. OPENDRIVE: ORB only. The blind 10:00 entry reads sign(price@10:00 -
   price@09:30) and nothing else. Segmenting the window by zigzag over 38 ES
   sessions:
       CHOP     13 traded  -3,756   38% win   median 5 legs, ER 0.15
       DRIVE     5 traded  -7,462   20% win   median 1 leg,  ER 0.42
       FAKEOUT  14 traded  -4,425   43% win   median 4 legs, ER 0.15
   Every shape loses and the CLEAN drives lose worst, so no shape filter
   rescues it -- only 5 of 38 windows are a single leg at all. The `orb` mode
   (wait for a real break of the 09:30-10:00 range) is the only positive
   opendrive variant on ES: +2,315 over 19 sessions.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from run_live import ALL_LABELS, _make  # noqa: E402


def test_zones_label_disables_break_but_keeps_the_runner():
    """RE-DERIVED on corrected fills, 23 sessions. Both cuts in 3f42771 were
    made on numbers produced by the broken fill model -- stops priced at the bar
    close, entries at the market while every managed level was measured from the
    zone edge:

        break   14 trips  -2,112  50% win     (the old numbers said -1,938 / 25%)
        runner  11 scaled trades: half off at +4 +7,262, RUNNER LEG +1,850

    The runner was cut for "gives back more than it makes" (-2,100 / 25% win).
    It makes +1,850. That cut was wrong and is reversed.

    Break still loses, so it stays off -- but as a small loss at a coin-flip win
    rate, not the 1-in-4 outlier the original number made it look like."""
    s = _make("zones", symbol="ES")
    assert s.enable_break is False, "the BREAK setup loses and is still enabled"
    assert s.runner is True, (
        "the runner is still disabled on the pre-fix numbers; the runner leg is "
        "+1,850 over 11 scaled trades once the fills are priced honestly")


def test_vwapbreak_label_enters_at_the_line():
    s = _make("vwapbreak", symbol="ES")
    assert s.entry_mode == "retest", f"vwapbreak still chases the break: {s.entry_mode}"


def test_opendrive_label_is_orb():
    s = _make("opendrive", symbol="ES")
    assert s.mode == "orb", f"opendrive still takes the blind 10:00 entry: {s.mode}"


def test_no_blind_drive_variant_survives_in_the_roster():
    """The 2p/2p16/2p24/2p32/2p_range/2p_retrace opendrive twins were all the
    blind entry with a different exit -- eight rows losing -15.8k to -19.8k each
    across 33 sessions, and one bet counted eight times in every portfolio
    number. Whatever remains must be an ORB."""
    for lb in ALL_LABELS:
        if not lb.startswith("opendrive"):
            continue
        s = _make(lb, symbol="ES")
        assert s.mode == "orb", f"{lb} still runs the blind drive entry"


def test_strategy_defaults_are_untouched_so_parity_still_holds():
    """The classes keep their validated defaults; only the roster changed."""
    from engine.strategies.open_drive import OpenDriveStrategy
    from engine.strategies.vwap_break import VwapBreakStrategy
    from engine.strategies.zones_strategy import ZoneLifecycleStrategy
    assert OpenDriveStrategy("ES").mode == "drive"
    assert VwapBreakStrategy("ES").entry_mode == "market"
    z = ZoneLifecycleStrategy("ES")
    assert z.enable_break is True and z.runner is True


def test_disabling_the_runner_takes_the_whole_position_at_the_scalp():
    """With the runner off the scalp must close everything, not half -- otherwise
    the remainder is still exposed with no runner logic to manage it."""
    from engine.core.events import Bar
    from engine.strategies.zones_strategy import ZoneLifecycleStrategy, _Trade
    s = ZoneLifecycleStrategy("ES", point_usd=50.0, runner=False)
    s.trade = _Trade("FADE", 1, 7000.0, 6990.0, 7020.0, 4, 7004.0, remaining=4)
    s.pos = 4
    out = s._manage(Bar(0, "30m", 7000, 7010, 6999, 7008, 100, "ES"))
    assert out, "scalp level touched but nothing closed"
    assert out[0].qty == 4, f"closed {out[0].qty} of 4 -- a runner was left behind"
    assert s.trade is None, "trade still open after a full scalp exit"


def test_only_one_of_the_correlated_onbreak_twins_is_deployed():
    """ONBREAK: three twins were the same bet counted three times.

    Pinned replay, 33 ES sessions, pairwise daily-P&L correlation:
        onbreak_2p24 ~ onbreak_2p32   +0.983
        onbreak_2p   ~ onbreak_2p32   +0.925
        onbreak_2p   ~ onbreak_2p24   +0.920
    All three are the SAME entry with a decay/range two-phase exit differing
    only in arming distance. On 2026-08-14 all five onbreak rows entered at
    10:22 and the family booked +3,650 of a +4,665 day -- one signal inflating
    the total fivefold and making the book look diversified.

    KEPT and why:
      onbreak          the raw entry, no two-phase. It LOSES (-1,488 over 23
                       sessions) while every two-phase twin makes money, which
                       is the contrast that says the exit is doing the work.
                       A control that disagrees with the deployed config is
                       worth more than a fourth copy that agrees.
      onbreak_2p24     one representative of the cluster.
      onbreak_2p_retrace  a genuinely different exit family (retrace, not
                       decay/range) -- absent from the >0.9 list.
      onbreak_gex      a different question: does the gamma gate help.

    2p32 scored best on this sample (+2,712 against 2p24's +1,075) and is NOT
    the one kept. Choosing the top scorer out of three ~0.95-correlated twins
    is selecting on the outcome; 24 is the arming distance already used as the
    retained control on opendrive and vwapbreak, so it is the choice made for a
    reason that is not this sample."""
    for gone in ("onbreak_2p", "onbreak_2p32"):
        assert gone not in ALL_LABELS, f"{gone} is a duplicate of onbreak_2p24"
    for kept in ("onbreak", "onbreak_2p24", "onbreak_2p_retrace", "onbreak_gex"):
        assert kept in ALL_LABELS, f"{kept} should still be deployed"
