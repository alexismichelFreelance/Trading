"""Top-level pytest config: the opt-in flag for the slow parity gate.

Shared test helpers live in tests/_helpers.py (a plain module) to avoid
ambiguity between the two conftest.py files on sys.path.
"""
from __future__ import annotations

import pytest


def pytest_addoption(parser):
    parser.addoption("--run-parity", action="store_true", default=False,
                     help="run the full QuestDB replay parity gate (slow)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-parity"):
        return
    skip = pytest.mark.skip(reason="parity gate: pass --run-parity to run")
    for item in items:
        if "parity" in item.keywords:
            item.add_marker(skip)
