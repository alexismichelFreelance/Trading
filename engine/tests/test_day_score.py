"""DayScore: the pure fade-friendliness combination + posture/label thresholds."""
from engine.features.day_score import WEIGHTS, DayScore, pctl


def test_pctl_trailing_fraction():
    assert pctl(50, [10, 20, 30, 40, 60]) == 0.8      # 4 of 5 below
    assert pctl(5, [10, 20, 30, 40, 60]) == 0.0
    assert pctl(50, [10, 20]) is None                 # need >=5
    assert pctl(None, [1, 2, 3, 4, 5]) is None


def test_fade_friendliness_weighted_mean():
    s = DayScore.fade_friendliness(crabel=1.0, overnight=1.0, volcalm=1.0)
    assert abs(s - 100.0) < 1e-9
    assert DayScore.fade_friendliness(crabel=0.0, overnight=0.0, volcalm=0.0) == 0.0
    # weighted: crabel .45, overnight .30, volcalm .25
    s = DayScore.fade_friendliness(crabel=1.0, overnight=0.0, volcalm=0.0)
    assert abs(s - 45.0) < 1e-9


def test_missing_components_renormalise():
    # only crabel available (at the open): score = crabel*100, weights renormalised
    assert abs(DayScore.fade_friendliness(crabel=0.7) - 70.0) < 1e-9
    # crabel + volcalm, no overnight: weights .45/.25 renormalise
    s = DayScore.fade_friendliness(crabel=1.0, volcalm=0.0)
    assert abs(s - 100 * 0.45 / (0.45 + 0.25)) < 1e-9
    assert DayScore.fade_friendliness() is None        # nothing available


def test_label_bands():
    assert "PRESS" in DayScore.label(70)
    assert DayScore.label(55) == "normal"
    assert "LIGHT" in DayScore.label(30)
    assert DayScore.label(None) == "n/a"


def test_posture_from_vwap_side():
    assert "support" in DayScore.posture(0.2)          # early above VWAP
    assert "resistance" in DayScore.posture(0.8)        # early below VWAP
    assert "both" in DayScore.posture(0.5)              # balanced
    assert DayScore.posture(None) == "unknown"


def test_weights_are_the_documented_tilts():
    assert WEIGHTS["crabel"] > WEIGHTS["overnight"] > WEIGHTS["volcalm"]
