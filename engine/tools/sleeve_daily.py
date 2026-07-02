"""Run one sleeve through the engine (both contracts) and dump per-trade records
to JSON for the portfolio report.

    python tools/sleeve_daily.py <ignition|opendrive|flow|zones> <out.json>

Configs are the DEPLOYABLE ones: ignition = trailing exit (causal, no regime),
opendrive = default, flow = default (abstract +-50 units; scaled at report time),
zones = default sized. CleanFill; costs applied at report time.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from run_replay import run_one  # noqa: E402

from engine.core.timeutil import et_session_date  # noqa: E402

KW = {
    "ignition": dict(exit_mode="trailing", trail_init=6.0, trail_width=10.0, regime_states=None),
    "opendrive": dict(),
    "flow": dict(),
    "zones": dict(),
}


def main() -> None:
    sleeve, out = sys.argv[1], sys.argv[2]
    recs = []
    for sym in ("ESM5", "ESH5"):
        blot = asyncio.run(run_one(sleeve, sym, **KW[sleeve]))
        for t in blot.trades:
            recs.append(dict(
                sym=sym,
                day=et_session_date(t.exit_ts if t.exit_ts else t.entry_ts),
                month=t.month,
                gross_points=t.gross_points,
                contracts=t.contracts,
            ))
    Path(out).write_text(json.dumps(recs), encoding="utf-8")
    print(f"{sleeve}: {len(recs)} trades -> {out}")


if __name__ == "__main__":
    main()
