"""GammaRegime feature (needs QuestDB + claude_gex loaded by tools/fetch_gex.py)."""
import pytest

from engine.adapters.questdb import QuestDB


def _ready() -> bool:
    try:
        q = QuestDB(timeout=5)
        return int(q.df("SELECT count() n FROM claude_gex")["n"].iloc[0]) > 1000
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _ready(), reason="claude_gex not loaded")


def test_gamma_regime_causal_and_sane():
    from engine.features.gamma import GammaRegime
    gr = GammaRegime()
    # April 8 2025 = post-crash, deeply short-gamma; May 15 = calm long-gamma
    assert gr.gexp_prev("2025-04-08") < 0.10
    assert gr.gexp_prev("2025-05-15") > 0.70
    assert gr.is_short_gamma("2025-04-08") is True
    assert gr.is_short_gamma("2025-05-15") is False
    # Monday uses Friday's session (weekend fallback)
    assert gr.gexp_prev("2025-03-24") is not None
    # before data begins -> None
    assert gr.gexp_prev("2010-01-04") is None
