"""Make repo root importable as `src.*` from tests."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _disable_prototype_lock(monkeypatch):
    """Render the legacy ``NN_*.py`` detail pages normally during tests.

    The legacy pages are locked to the Build surface in the prototype
    (``app/prototype_mode.lock_to_build_page``, gated on
    ``DK_LAB_PROTOTYPE_LOCK``, default on). The existing page AppTests exercise
    those pages' real UI, so the suite turns the lock **off** session-wide here.
    The lock's own behavior is covered by ``tests/test_prototype_page_lock.py``,
    which re-enables it explicitly via ``monkeypatch.setenv(..., "1")``.
    """
    monkeypatch.setenv("DK_LAB_PROTOTYPE_LOCK", "0")
