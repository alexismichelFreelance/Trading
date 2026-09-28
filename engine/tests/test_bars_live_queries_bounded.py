"""No query on the ENGINE's path may scan claude_bars_live unbounded.

2026-09-07, during RTH. The user recompiled the NT8 overlay and restarted the
engine, and the chart went blank -- no panel, no zones, no position tracking.
None of it was a painter fault. BasisSeries.load ran

    SELECT ts, c FROM claude_bars_live WHERE symbol = 'ES' ORDER BY ts

to compute a THIRTY DAY basis. It is called synchronously on the asyncio event
loop, at startup and again on every session rollover -- and a restart mid-session
replays several days of backfill, so the rollover path fired repeatedly. Each
call took the full QuestDB timeout and froze the loop, so the engine never got
back to painting anything.

WHY THIS TEST AND NOT JUST THE FIX. The identical pattern had been found and
fixed in tools/portfolio_replay.py the day before. The instance was fixed; the
CLASS was not swept for. The .ncd backfill took this table from ~57 sessions to
1.58M ES rows, and every unbounded query written when it was small became a
latent freeze. This test is the sweep, so the next one is caught by CI instead of
by a blank chart in the middle of a session.

Scope is deliberately the engine's own path -- engine/ and the live runner.
Research scripts in tools/ may scan the whole table; that is what they are for,
they run in a terminal, and nobody's session depends on them returning.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Files whose queries run inside the live engine, on its event loop.
ENGINE_PATH = [
    *(ROOT / "engine").rglob("*.py"),
    ROOT / "tools" / "run_live.py",
]

# A bound is any clause that limits the rows: a time window, or a LIMIT.
BOUNDED = re.compile(
    r"dateadd\s*\(|ts\s*(>=|>)\s*[`'\"{]|LIMIT\s|latest on|SAMPLE BY|"
    r"AND\s+ts\s*(>=|>)|\{lo\}|\{_lo\}",
    re.IGNORECASE)


def _statements(text: str) -> list[str]:
    """Each SELECT ... FROM claude_bars_live ... with the source lines that
    follow it, since these queries are built from adjacent f-string fragments."""
    out = []
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if "FROM claude_bars_live" in ln or "claude_bars_live" in ln and "SELECT" in ln:
            out.append(" ".join(lines[i:i + 6]))
    return out


@pytest.mark.parametrize("path", ENGINE_PATH, ids=lambda p: p.name)
def test_engine_path_never_scans_claude_bars_live_unbounded(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    if "claude_bars_live" not in text:
        pytest.skip("no claude_bars_live query")
    unbounded = [s for s in _statements(text) if not BOUNDED.search(s)]
    assert not unbounded, (
        f"{path.relative_to(ROOT)} queries claude_bars_live with no row bound. "
        f"That table holds 1.58M ES rows since the .ncd backfill; an unbounded "
        f"scan times out and, on the engine's event loop, freezes everything "
        f"including painting. Add a dateadd() window or a LIMIT.\n  "
        + "\n  ".join(s.strip()[:160] for s in unbounded))


def test_basis_load_asks_only_for_the_window_it_uses():
    """BasisSeries.load takes `days` and must actually apply it to the query."""
    from engine.features.gamma_basis import BasisSeries

    seen: list[str] = []

    class FakeQ:
        def df(self, sql):
            seen.append(sql)
            import pandas as pd
            return pd.DataFrame()

    BasisSeries.load(FakeQ(), "SPX", "ES", 42.0, days=30)
    bars = [s for s in seen if "claude_bars_live" in s]
    assert bars, "expected a bars query"
    assert "dateadd" in bars[0], (
        "the bars query must be time-bounded; an unbounded scan of this table "
        "froze the live engine on 2026-09-07")
