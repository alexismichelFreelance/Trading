"""Every label in config/live.yaml must actually build.

2026-08-12 09:49, at the open:

    engine starting -- log file D:\Trading\engine\logs\engine.log
    unknown strategy 'opendrive_orb'

and the engine exited. `opendrive_orb` was removed from _make on 2026-08-11
because `opendrive` now builds mode="orb", making the two the same config. I
checked ALL_LABELS and never checked the file the live runner actually reads.

Two separate lists of strategy names, nothing keeping them in agreement, and the
one under test was not the one in production.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from run_live import ALL_LABELS, _make  # noqa: E402

CFG = ROOT / "config" / "live.yaml"


def _lanes():
    cfg = yaml.safe_load(CFG.read_text(encoding="utf-8")) or {}
    lanes = cfg.get("instruments", cfg)
    return [(s, l) for s, l in lanes.items() if isinstance(l, dict)]


def test_every_configured_label_builds():
    """THE REGRESSION."""
    bad = []
    for sym, lane in _lanes():
        for key in ("paper", "live"):
            spec = str(lane.get(key) or "")
            if spec in ("all", ""):
                continue
            for raw in (x.strip() for x in spec.split(",")):
                if not raw or raw == "all":
                    continue
                # '-label' SUBTRACTS from the roster (see build_roster). It is
                # not a sleeve to construct -- but it must still NAME one, or the
                # cut silently does nothing. At runtime a typo is deliberately
                # non-fatal, so this is the place that has to catch it.
                if raw.startswith("-"):
                    if raw[1:] not in ALL_LABELS:
                        bad.append(f"{sym}.{key}: {raw!r} subtracts a sleeve "
                                   f"that does not exist -- the cut is a no-op")
                    continue
                try:
                    _make(raw, symbol=sym)
                except BaseException as ex:            # SystemExit included
                    bad.append(f"{sym}.{key}: {raw!r} -> {ex}")
    assert not bad, "config/live.yaml names strategies that do not exist:\n  " + \
                    "\n  ".join(bad)


def test_live_subset_is_contained_in_the_paper_roster():
    """A label routed LIVE that is not paper-traded is never measured before it
    risks money."""
    for sym, lane in _lanes():
        paper = str(lane.get("paper") or "")
        live = [x.strip() for x in str(lane.get("live") or "").split(",") if x.strip()]
        if paper == "all" or not live:
            continue
        pset = {x.strip() for x in paper.split(",")}
        missing = [x for x in live if x not in pset]
        assert not missing, f"{sym}: live-only labels {missing} are not in the paper roster"


def test_all_labels_also_build():
    """The roster the runner defaults to, checked alongside the config so the two
    cannot drift apart again."""
    from run_live import ALL_LABELS
    bad = []
    for lb in ALL_LABELS:
        try:
            _make(lb, symbol="ES")
        except BaseException as ex:
            bad.append(f"{lb!r} -> {ex}")
    assert not bad, "ALL_LABELS contains unbuildable labels:\n  " + "\n  ".join(bad)


# ── one dead label must not cost the whole book ──────────────────────────────
#
# build_roster called _make in a bare loop, and _make raises SystemExit for an
# unknown label. So one stale name in a ten-sleeve roster killed the engine
# before it placed a single order -- nine working sleeves stood down because of
# a tenth that no longer existed.
#
# Same reasoning as the dispatch isolation fix on 2026-08-05: the blast radius
# of a broken sleeve is that sleeve. A roster entry that cannot be built is
# dropped, LOUDLY, and the rest of the book trades.

def test_an_unknown_label_drops_that_sleeve_and_keeps_the_rest(caplog):
    import logging
    from run_live import build_roster
    with caplog.at_level(logging.ERROR):
        roster = build_roster("zones,definitely_not_a_sleeve,pivot", symbol="ES")
    names = [lb for lb, _ in roster]
    assert names == ["zones", "pivot"], f"roster is {names}"
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "definitely_not_a_sleeve" in msg, "the dropped sleeve was not named"


def test_a_roster_of_only_bad_labels_still_fails_loudly():
    """Dropping every sleeve silently would start an engine that cannot trade."""
    from run_live import build_roster
    try:
        build_roster("nope_one,nope_two", symbol="ES")
    except SystemExit as ex:
        assert "nope_one" in str(ex) or "no sleeves" in str(ex).lower()
    else:
        raise AssertionError("an entirely unbuildable roster started anyway")


def test_a_good_roster_is_unchanged():
    from run_live import build_roster
    r = build_roster("zones,pivot,vwapbreak", symbol="ES")
    assert [lb for lb, _ in r] == ["zones", "pivot", "vwapbreak"]


def test_nq_runs_trendjoin():
    """trendjoin is the best sleeve on NQ and was never deployed there.

    Replay over 23 recorded NQ sessions, de-duplicated to one sleeve per bet
    (strategy_lab/book_candidates.py):

        NQ:trendjoin_narrow   23 days  +36,925  1,605/day  70% positive
        NQ:opendrive_2p24     23       +20,430    888/day  91%
        NQ:zones_gap           7       +15,215  2,174/day  57%
        NQ:flow               17        +7,760    456/day  65%

    against the whole ES book at 717/day. Normalised for size -- NQ ranges ~465
    points a session against ES's ~65 -- trendjoin captures ~17% of the daily
    range on NQ and ~9.5% on ES, so this is a real difference and not just a
    bigger instrument.

    instruments.yaml has carried NQ-specific trend_join parameters (conf_pts
    186, stop_pts 53, derived from NQ's own range) since the multi-instrument
    work. They had never been used by anything.

    NOT claimed: that this survives. 23 sessions is one month, the same window
    the ES book was measured on, and ES:opendrive_2p24 went from the top sleeve
    to nothing when 13 sessions were added. This is deployed to PAPER."""
    import yaml
    cfg = yaml.safe_load(open(ROOT / "config" / "live.yaml", encoding="utf-8"))
    nq = cfg["instruments"]["NQ"]["paper"]
    labels = [x.strip() for x in nq.split(",")]
    assert "trendjoin" in labels, f"NQ roster has no trendjoin: {labels}"
    assert "trendjoin_narrow" in labels
    assert nq_lane_builds(labels)


def nq_lane_builds(labels) -> bool:
    """Every NQ label must actually construct -- a stale name takes the whole
    lane down at the open (2026-08-12, 'opendrive_orb')."""
    from run_live import _make
    for lb in labels:
        _make(lb, symbol="NQ")
    return True


# ── cutting a sleeve from a lane that runs 'all' ────────────────────────────
# ES runs `paper: all` so it picks up new sleeves automatically. Cutting one
# used to mean freezing the lane to a hand-written list, which drifts from
# ALL_LABELS -- and this file already records what a stale roster name costs
# (2026-08-12, one dead label took nine working sleeves down at the open).
# `-label` subtracts instead, so the lane stays on 'all' and the exclusion is
# explicit and auditable in config/live.yaml.
def test_all_minus_a_label_drops_only_that_label():
    from run_live import ALL_LABELS, build_roster
    full = build_roster("all", symbol="ES")
    cut = build_roster("all,-flow,-ignition", symbol="ES")
    names_full = {lb for lb, _ in full}
    names_cut = {lb for lb, _ in cut}
    assert "flow" in names_full and "ignition" in names_full
    assert "flow" not in names_cut and "ignition" not in names_cut
    assert names_full - names_cut == {"flow", "ignition"}, \
        "subtracting must remove EXACTLY the named labels"
    # the variants survive: only the base sleeve was cut
    assert "flow_lg" in names_cut and "ignition_fixed" in names_cut


def test_subtraction_works_on_an_explicit_list_too():
    from run_live import build_roster
    r = build_roster("zones,pivot,flow,-flow", symbol="ES")
    names = {lb for lb, _ in r}
    assert names == {"zones", "pivot"}


def test_dropping_an_unknown_label_is_not_fatal():
    """Same rule as a stale positive label: it costs its own sleeve, nothing
    more. A typo in a `-` entry must not silently take the lane down."""
    from run_live import build_roster
    r = build_roster("all,-definitely_not_a_sleeve", symbol="ES")
    assert len(r) > 10
