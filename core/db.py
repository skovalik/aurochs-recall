"""SQLite connection factory.

Every connection MUST go through ``connect()``. The pragmas applied here
are not optional — leaving any of them off changes correctness, not just
performance.

* ``foreign_keys = ON`` — SQLite default is OFF. Without this, every FK
  declared in the schema is documentation, not enforcement.
* ``journal_mode = WAL`` — concurrent readers + one writer; required for
  the OS-lockfile concurrency model.
* ``wal_autocheckpoint = 1000`` — keeps the WAL bounded under MCP burst.
* ``busy_timeout = 30000`` — 30s cap before raising ``database is locked``.

Connections are NOT thread-safe by default. Multiprocessing workers MUST
call ``connect()`` themselves rather than passing a connection across the
process boundary — pickling a sqlite3 Connection is not supported and
fork-sharing would corrupt the WAL.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Pragmas applied to every connection, in order. Read by tests to assert
# the contract (test_schema enforces ``foreign_keys = ON``).
_REQUIRED_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("foreign_keys", "ON"),
    ("journal_mode", "WAL"),
    ("wal_autocheckpoint", "1000"),
    ("busy_timeout", "30000"),
)


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a sqlite3 Connection with the recall pragma contract applied.

    Creates the database file if it does not exist. The caller owns the
    connection — close it (or use as a context manager) when done.

    Parameters
    ----------
    db_path:
        Path to the recall database file. Must be a real filesystem path
        (``:memory:`` is supported for tests; pass it as a literal str).
    """
    if isinstance(db_path, str) and db_path == ":memory:":
        target: str | Path = ":memory:"
    else:
        target = Path(db_path)
        # Ensure the parent dir exists for fresh installs.
        if isinstance(target, Path):
            target.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(target),
        # Allow per-call detect_types=False (default); the schema stores
        # everything as INTEGER/TEXT/BLOB and dataclasses handle parsing.
        isolation_level=None,  # autocommit; we manage transactions manually
        check_same_thread=True,
    )
    conn.row_factory = sqlite3.Row

    for pragma, value in _REQUIRED_PRAGMAS:
        # journal_mode returns the new mode as a result row; the others
        # don't. We don't care about the return value, only that the
        # statement succeeds.
        conn.execute(f"PRAGMA {pragma} = {value}")

    # Verify foreign_keys actually took (some sqlite builds ignore the
    # pragma inside a transaction). Fail loud if it didn't.
    enforced = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if enforced != 1:
        conn.close()
        raise RuntimeError(
            "PRAGMA foreign_keys = ON failed to apply. This sqlite build "
            "may not support FK enforcement; refusing to continue."
        )

    return conn
