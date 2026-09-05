"""attach_level_books must only feed sleeves that actually own a LevelBook.

The engine failed to start on 2026-08-31 with

    AttributeError: 'SessionLevels' object has no attribute 'set_gamma'

The sleeve filter was `getattr(s, "levels", None) is not None`, but `.levels`
is not a single type: ignition sets `self.levels = SessionLevels(...)`, a
different class that happens to share the attribute name and has no set_gamma.
Only trendjoin_lvl carries a LevelBook. Selecting on the attribute NAME rather
than on the capability took the whole engine down on startup.

Second fault in the same function, which the first one hid: QuestDB was called
but never imported, so every attempt to load the walls raised NameError into
the fail-open handler. The book then ran without the one source it exists to
wait for -- the failure mode its own docstring warns about, a level source that
is wired but never fed.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.features.level_book import LevelBook          # noqa: E402
from engine.features.pivots import SessionLevels          # noqa: E402
from tools.run_live import attach_level_books             # noqa: E402


class Sleeve:
    def __init__(self, levels=None):
        self.levels = levels


def test_sessionlevels_sleeve_does_not_crash_startup():
    """ignition owns a SessionLevels. It must be skipped, not fed."""
    s = Sleeve(SessionLevels(round_step=50.0))
    n = attach_level_books([s], "ES", "2026-08-30")     # must not raise
    assert n == 0
    assert not hasattr(s.levels, "gamma")


def test_mixed_roster_feeds_only_the_level_book():
    lb, sl = Sleeve(LevelBook()), Sleeve(SessionLevels())
    none_ = Sleeve(None)
    n = attach_level_books([sl, lb, none_], "ES", "2026-08-30")
    assert n == 1
    assert hasattr(lb.levels, "gamma")                  # set_gamma was called


def test_gamma_load_failure_is_not_a_NameError(caplog):
    """Fail-open is for a missing DB, not for a missing import."""
    with caplog.at_level(logging.WARNING):
        attach_level_books([Sleeve(LevelBook())], "ES", "2026-08-30")
    for rec in caplog.records:
        assert "is not defined" not in rec.getMessage(), (
            f"gamma walls failed on a NameError, not on the data: "
            f"{rec.getMessage()}")
