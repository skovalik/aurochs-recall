"""SQLite connection factory (delegated to aurochs-core).

The connection helper ``db_connect()`` is now provided by
``aurochs-core``; see ``aurochs_core.db`` for the canonical pragma
contract (``foreign_keys=ON``, ``journal_mode=WAL``,
``synchronous=NORMAL``, ``busy_timeout=30000``, ``isolation_level=None``).
We re-export it here so existing
``from aurochs_recall.core.db import db_connect`` import sites keep
working, and provide a ``connect`` alias for backward compatibility
with any module that still imports the legacy name.

The pragma profile applied via ``aurochs_core.db_connect`` differs
from recall's earlier local profile in two ways:

* ``synchronous = NORMAL`` is now applied (was implicit FULL before).
  WAL-safe and roughly 2x cheaper on bulk inserts.
* ``wal_autocheckpoint`` is no longer set explicitly; SQLite's default
  (~1000 pages) is the same value recall used to set, so behavior is
  unchanged at the autocheckpoint boundary.

Connections are NOT thread-safe by default. Multiprocessing workers
MUST call ``db_connect()`` themselves rather than passing a connection
across the process boundary — pickling a sqlite3 Connection is not
supported and fork-sharing would corrupt the WAL.
"""

from __future__ import annotations

from aurochs_core import db_connect

# Backwards-compatible alias so any straggling
# ``from aurochs_recall.core.db import connect`` imports keep working.
# Prefer the canonical ``db_connect`` name in new code.
connect = db_connect

__all__ = ["connect", "db_connect"]
