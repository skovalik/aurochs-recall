"""Database integrity verification + recovery hooks.

``verify_or_rebuild`` is called from :class:`core.index.Indexer.__init__` so
every fresh indexer process catches WAL-recovery-needed cases before
hitting the write path. It runs ``PRAGMA integrity_check`` and, on failure,
raises a structured ``CorruptionDetected`` error with the documented
manual-recovery steps.

The "rebuild" half of the name is aspirational for T0 — we don't auto-rebuild
yet because doing so without explicit user consent could mask real data loss.
A later patch will add a ``recall verify --rebuild`` CLI surface that calls
into this module with explicit opt-in.
"""

from __future__ import annotations

from pathlib import Path

from aurochs_recall.core.db import db_connect


class CorruptionDetected(RuntimeError):
    """Raised when ``PRAGMA integrity_check`` returns anything other than ``ok``.

    Carries the raw integrity-check output and a documented recovery path
    so the caller can render a useful CLI message.
    """

    def __init__(self, db_path: Path, integrity_report: list[str]) -> None:
        self.db_path = db_path
        self.integrity_report = integrity_report
        super().__init__(
            f"Database integrity check failed for {db_path}.\n"
            f"Report: {integrity_report!r}\n"
            "Recovery options (manual, in order of safety):\n"
            "  1. Restore from the most recent successful `recall backup`.\n"
            "  2. If no backup is available, run `sqlite3 recall.db .recover "
            "| sqlite3 recall.db.recovered`, then `recall verify --deep` on "
            "the recovered file before swapping it in.\n"
            "  3. As a last resort, delete the database and re-index from "
            "sources (drawer_uid stability means the new database will have "
            "the same identities)."
        )


def verify_or_rebuild(db_path: Path | str) -> None:
    """Run ``PRAGMA integrity_check``; raise on corruption.

    The function is a no-op for databases that don't exist yet — fresh
    installs will run migrations next, which creates the file. For
    existing databases, it runs the integrity check and only returns
    cleanly if the result is exactly ``ok``.

    Parameters
    ----------
    db_path:
        Path to the recall database file.

    Raises
    ------
    CorruptionDetected:
        If the integrity check returns anything other than ``ok``.
    """
    path = Path(db_path)
    if not path.exists():
        # Fresh install — nothing to verify yet.
        return

    conn = db_connect(path)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        # integrity_check returns one row containing the literal string 'ok'
        # for healthy databases, or one or more rows describing problems.
        report = [r[0] for r in rows]
        if report != ["ok"]:
            raise CorruptionDetected(path, report)
    finally:
        conn.close()
