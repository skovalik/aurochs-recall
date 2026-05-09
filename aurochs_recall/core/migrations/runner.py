"""Sequential migration runner.

Spec from plan v4 / v5:

1. Acquire ``MigrateLock`` (fail fast if held).
2. ``BEGIN EXCLUSIVE`` transaction.
3. Verify the migration version is exactly ``current_version + 1`` (no skips).
4. Insert ``schema_version`` row with status ``in_progress``.
5. Apply each statement individually.
6. On success, flip status to ``applied`` and commit.
7. On failure, ``ROLLBACK`` — schema_version stays at ``in_progress`` so the
   next run can detect the partial state.

For T0 only ``0001_initial.sql`` exists, so the runner's job reduces to
"apply the baseline if no schema_version row is present." Future versions
follow the full sequential path.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from aurochs_core import MigrateLock

from aurochs_recall.core.db import db_connect
from aurochs_recall.core.schema import (
    CURRENT_SCHEMA_VERSION,
    apply_schema,
    current_schema_version,
)

# Conservative lock timeout — the migration runner shouldn't block forever.
_LOCK_TIMEOUT_SECONDS: float = 60.0

_VERSION_RE = re.compile(r"^(?P<v>\d{4})_")


class MigrationError(RuntimeError):
    """Raised on out-of-order migrations or partial-state detection."""


def discover_migrations(migrations_dir: Path | None = None) -> list[tuple[int, Path]]:
    """Enumerate all migration files, sorted by numeric version prefix.

    Files must be named ``NNNN_description.sql`` where NNNN is a zero-padded
    integer. Returns a list of ``(version, path)`` tuples in ascending order.
    """
    base = migrations_dir or (Path(__file__).parent)
    out: list[tuple[int, Path]] = []
    for sql_file in sorted(base.glob("*.sql")):
        match = _VERSION_RE.match(sql_file.name)
        if not match:
            continue
        out.append((int(match.group("v")), sql_file))
    return out


def run_migrations(
    db_path: Path | str,
    *,
    target: int | None = None,
    description: str | None = None,
) -> int:
    """Apply pending migrations up to ``target`` (or latest available).

    Returns the version the database is at after this call. Acquires
    ``MigrateLock`` for the duration; second concurrent migrator either
    waits up to ``_LOCK_TIMEOUT_SECONDS`` or fails with ``LockError``.

    For T0 the only migration is ``0001_initial.sql``. Calling this on a
    fresh database applies the baseline and records schema_version=1. On
    an already-migrated database it's a no-op.
    """
    target_version = target if target is not None else CURRENT_SCHEMA_VERSION
    db = Path(db_path)

    available = discover_migrations()
    if not available:
        raise MigrationError("No migration files found in core/migrations/")

    with MigrateLock(db, timeout=_LOCK_TIMEOUT_SECONDS):
        conn = db_connect(db)
        try:
            applied = current_schema_version(conn)

            if applied >= target_version:
                # Already at or beyond target — nothing to do.
                return applied

            # Detect partial state: any 'in_progress' row means a previous
            # run was interrupted. Only safe to query if the table exists.
            schema_version_exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='schema_version'"
            ).fetchone()
            if schema_version_exists is not None:
                in_prog = conn.execute(
                    "SELECT version FROM schema_version WHERE status='in_progress'"
                ).fetchone()
                if in_prog is not None:
                    raise MigrationError(
                        f"Detected partial migration at version {in_prog[0]}. "
                        "Manual recovery required: inspect schema_version, then "
                        "re-run migration after rolling forward or dropping "
                        "the partial state."
                    )

            for version, path in available:
                if version <= applied:
                    continue
                if version > target_version:
                    break
                if version != applied + 1:
                    raise MigrationError(
                        f"Out-of-order migration: have v{applied}, "
                        f"expected v{applied + 1}, got v{version}"
                    )
                _apply_one(conn, version, path, description)
                applied = version

            return applied
        finally:
            conn.close()


def _apply_one(
    conn: sqlite3.Connection,
    version: int,
    path: Path,
    description: str | None,
) -> None:
    """Apply a single migration file under BEGIN EXCLUSIVE.

    Records ``in_progress`` before running, flips to ``applied`` on success.
    """
    sql_text = path.read_text(encoding="utf-8")

    # Detect baseline-application: the schema_version table itself doesn't
    # exist on a virgin DB until v1's DDL creates it. We can't pre-stamp
    # ``in_progress`` until that table is in place. This is a structural
    # property of v1, not a property of "version == latest" — so detect
    # by table absence directly. (Earlier versions of this code keyed on
    # ``version == CURRENT_SCHEMA_VERSION``, which broke when newer
    # migrations were added: applying v1 on a virgin DB stopped matching
    # the baseline branch and tried to INSERT into a not-yet-created
    # schema_version table.)
    schema_version_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone() is not None

    if not schema_version_exists:
        # Baseline path: defer to apply_schema which executes the DDL
        # (creating schema_version inside it) and inserts the row via
        # OR IGNORE. Reuse that rather than duplicate.
        apply_schema(conn, version=version, description=description)
        return

    # NOTE on transactions: ``executescript`` issues an implicit COMMIT
    # before running the script, so we cannot wrap the whole flow in a
    # single BEGIN EXCLUSIVE/COMMIT pair. Instead we record ``in_progress``
    # first (auto-committed thanks to the connection's autocommit
    # isolation_level=None setting), then run the DDL script, then flip
    # to ``applied``. If the process crashes mid-DDL the schema_version
    # row stays at ``in_progress`` — which is exactly what the partial-
    # state detection branch in run_migrations() looks for on next start.
    try:
        # OR REPLACE so a previous 'failed' row for the same version is
        # overwritten cleanly. The PRIMARY KEY on schema_version.version
        # would otherwise reject the second attempt.
        conn.execute(
            "INSERT OR REPLACE INTO schema_version "
            "(version, applied_at, description, status) "
            "VALUES (?, ?, ?, 'in_progress')",
            (version, int(time.time()), description or path.stem),
        )
        conn.executescript(sql_text)
        conn.execute(
            "UPDATE schema_version SET status='applied' WHERE version=?",
            (version,),
        )
    except Exception:
        # Best-effort: try to flip the row to 'failed' so operators can see
        # which version partial-applied. If even that fails, leave it at
        # 'in_progress' — run_migrations() will refuse to proceed and
        # require manual recovery.
        try:
            conn.execute(
                "UPDATE schema_version SET status='failed' WHERE version=?",
                (version,),
            )
        except sqlite3.OperationalError:
            pass
        raise
