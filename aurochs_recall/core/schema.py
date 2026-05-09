"""Schema-application helper.

The actual DDL lives in ``core/migrations/0001_initial.sql``. This module
loads that file and applies it to a connection, idempotently. Use this for
fresh-database setup and tests; production upgrades go through
``core.migrations.runner.run_migrations`` which records ``schema_version``
rows.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

# Current schema version embedded in the spine. Future migrations add files
# named ``000N_description.sql`` and bump this constant.
#
# v1 (0001_initial.sql) — drawer_meta + drawers_fts + entities + relationships +
#                          predicates + index_state + ingest_errors + schema_version
# v2 (0002_t1_extraction.sql) — extract_pending + extraction_runs (T1 BYOK)
CURRENT_SCHEMA_VERSION: int = 2

_MIGRATIONS_DIR: Path = Path(__file__).parent / "migrations"


def schema_path(version: int = CURRENT_SCHEMA_VERSION) -> Path:
    """Return the filesystem path to the SQL file for a schema version."""
    candidates = sorted(_MIGRATIONS_DIR.glob(f"{version:04d}_*.sql"))
    if not candidates:
        raise FileNotFoundError(
            f"No migration file found for schema version {version} in {_MIGRATIONS_DIR}"
        )
    if len(candidates) > 1:
        raise RuntimeError(
            f"Multiple migration files for version {version}: {candidates!r}"
        )
    return candidates[0]


def apply_schema(
    conn: sqlite3.Connection,
    version: int = CURRENT_SCHEMA_VERSION,
    description: str | None = None,
) -> None:
    """Apply DDL up to and including ``version``, idempotently.

    Cumulative: applies every migration file in order from v1 through
    ``version``. All statements use ``CREATE TABLE IF NOT EXISTS`` /
    ``INSERT OR IGNORE`` so re-running this on an already-initialized
    database is a no-op. After each version's DDL runs, a row is
    recorded in ``schema_version`` (``OR IGNORE`` so re-application is
    silent).

    This is the test-helper / fresh-install entry point. Production
    upgrades go through ``core.migrations.runner.run_migrations`` which
    enforces single-writer locking and partial-state detection.

    Parameters
    ----------
    conn:
        Open sqlite3 Connection (preferably one from ``core.db.db_connect``).
    version:
        Highest schema version to apply. Defaults to ``CURRENT_SCHEMA_VERSION``.
        All earlier versions are applied first.
    description:
        Optional human-readable description recorded in the
        ``schema_version`` row for the highest version applied.
    """
    # NOTE: ``executescript`` issues its own COMMIT before running and after
    # finishing, so we cannot wrap it in our own BEGIN/COMMIT — sqlite3 will
    # raise "cannot commit - no transaction is active" on the explicit
    # COMMIT. Partial-failure rollback within executescript is handled by
    # sqlite itself: any error mid-script raises and leaves the database
    # in a consistent state because each CREATE / INSERT is its own
    # transaction at the sqlite level.
    for v in range(1, version + 1):
        sql_text = schema_path(v).read_text(encoding="utf-8")
        conn.executescript(sql_text)
        # The schema_version table is created by v1; safe to insert here for
        # every version since v1 is always applied first when version >= 1.
        desc = description if v == version else f"baseline v{v}"
        conn.execute(
            "INSERT OR IGNORE INTO schema_version "
            "(version, applied_at, description, status) VALUES (?, ?, ?, 'applied')",
            (v, int(time.time()), desc or f"baseline v{v}"),
        )


def current_schema_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied schema version in the database, or 0.

    Returns 0 if the schema_version table doesn't exist yet (fresh DB) or
    if no successful migration has been recorded.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    result = conn.execute(
        "SELECT MAX(version) FROM schema_version WHERE status='applied'"
    ).fetchone()
    return result[0] or 0
