"""Every macrodip sleeve in the roster must have a warm seed.

The NQ seed was never generated. macro_dip logs the miss and returns [], so the
sleeve loads, appears in the roster, reports healthy -- and cannot trade for a
year, because its 250-day dip percentile never warms. A sleeve that is present
but structurally unable to fire is the worst kind of silent failure: the roster
says it is being tested and it is not.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.strategies.macro_dip import LOOKBACK, PCTL_WIN   # noqa: E402

NEED = PCTL_WIN + LOOKBACK


def test_every_symbol_with_a_macrodip_sleeve_has_a_warm_seed():
    import tools.run_live as RL
    missing = []
    for sym in ("ES", "NQ"):
        spec = RL.INSTRUMENTS.get(sym)
        if spec is None:
            continue
        hmm = str(RL.ROOT / spec.hmm_path) if spec.hmm_path else RL.HMM_PATH
        roster = RL.build_roster("all", flow_th=None, symbol=sym,
                                 hmm_path=hmm, prefix="")
        if not any(type(s).__name__ == "MacroDipStrategy" for _, s in roster):
            continue
        p = ROOT / "config" / f"macrodip_seed_{sym}.json"
        if not p.exists():
            missing.append(f"{sym}: no seed at {p}")
            continue
        closes = json.loads(p.read_text())["closes"]
        if len(closes) < NEED:
            missing.append(f"{sym}: {len(closes)} closes, needs >= {NEED}")
    assert not missing, "regenerate with tools/gen_macrodip_seed.py -- " + "; ".join(missing)
