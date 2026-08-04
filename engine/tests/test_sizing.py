from engine.strategies.sizing import position_size


def test_risk_based_size_es():
    # $2,000 risk, 4-pt stop, ES $50/pt -> floor(2000/200) = 10
    assert position_size(2000, 4, 50.0) == 10


def test_size_caps_at_max():
    # tiny stop would size huge -> capped at 30
    assert position_size(2000, 0.5, 50.0, max_contracts=30) == 30


def test_size_zero_on_bad_stop():
    assert position_size(2000, 0, 50.0) == 0
    assert position_size(0, 4, 50.0) == 0


def test_size_floor_behaviour():
    # 2000 / (12 * 50) = 3.33 -> 3
    assert position_size(2000, 12, 50.0) == 3
