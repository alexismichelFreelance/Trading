"""Instrument registry: yaml loading, root resolution, options escape hatch."""
from pathlib import Path

from engine.core.config import InstrumentSpec, load_instruments, root_symbol

ROOT = Path(__file__).resolve().parents[1]
YAML = ROOT / "config" / "instruments.yaml"


def test_load_instruments():
    specs = load_instruments(YAML)
    assert set(specs) >= {"ES", "NQ", "GC"}
    es = specs["ES"]
    assert es.point_usd == 50.0 and es.tick == 0.25
    assert es.tick_usd == 12.5
    assert es.asset_class == "future"
    assert es.hmm_path == "config/hmm_es_1h.json"
    assert es.round_step == 50.0
    # unknown yaml keys land in extra, not TypeError
    assert es.extra["replay"]["sec_tables"]["ESM5"] == "claude_sec_feat"
    assert specs["NQ"].point_usd == 20.0
    assert specs["GC"].tick == 0.10


def test_root_symbol_known_registry():
    known = ["ES", "NQ", "GC", "MES", "MNQ"]
    assert root_symbol("ESM5", known) == "ES"
    assert root_symbol("ESH5", known) == "ES"
    assert root_symbol("MESM5", known) == "MES"   # longest prefix wins
    assert root_symbol("MNQZ25", known) == "MNQ"
    assert root_symbol("GCQ6", known) == "GC"


def test_root_symbol_month_code_fallback():
    # no registry: CME month-code pattern
    assert root_symbol("ESM5") == "ES"
    assert root_symbol("MESM5") == "MES"
    assert root_symbol("NQH26") == "NQ"
    assert root_symbol("ZNU5") == "ZN"
    # non-contract string: legacy 2-letter slice
    assert root_symbol("ES") == "ES"


def test_spec_defaults_session():
    s = InstrumentSpec("XX", 1.0, 0.01)
    assert s.rth_start_min == 570 and s.rth_end_min == 960
    assert s.extra == {}
