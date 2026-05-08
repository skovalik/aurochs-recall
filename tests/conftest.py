"""Shared pytest configuration.

Adds the project root to ``sys.path`` so tests can ``import core`` without
a package install. When the spine ships ``pyproject.toml`` with editable
install support this can go away — until then it lets the test suite run
straight from a clean checkout.

Also exposes the search-fixture DB to all tests via the ``fixture_db_path``
and ``fixture_conn`` pytest fixtures.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


FIXTURE_DB = ROOT / "tests" / "fixtures" / "search" / "recall.db"


def _ensure_fixture() -> None:
    """Build the fixture if it doesn't exist (CI-safe lazy build)."""
    if FIXTURE_DB.exists():
        return
    subprocess.run(
        [sys.executable, "-m", "tests.fixtures.search.build_fixture"],
        cwd=str(ROOT),
        check=True,
    )


@pytest.fixture(scope="session")
def fixture_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Copy the deterministic fixture DB to a tmp path so each session is
    isolated from on-disk fixture mutations."""
    _ensure_fixture()
    out = tmp_path_factory.mktemp("recall_fixture") / "recall.db"
    shutil.copyfile(FIXTURE_DB, out)
    return out


@pytest.fixture
def fixture_conn(fixture_db_path: Path):
    conn = sqlite3.connect(str(fixture_db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
