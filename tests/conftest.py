"""Shared pytest configuration.

Adds the project root to ``sys.path`` so tests can ``import core`` without
a package install. When the spine ships ``pyproject.toml`` with editable
install support this can go away — until then it lets the test suite run
straight from a clean checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
