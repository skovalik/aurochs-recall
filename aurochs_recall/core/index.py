"""Drawer indexer.

The indexer reads from ingestors and writes drawers into the database. It
holds a ``WriteLock`` for the entire run, batches inserts in transactions
of 1000, and dedupes via the ``content_hash + source + source_id`` UNIQUE
constraint.

Ingest-error reporting
----------------------
The schema's ``ingest_errors`` table records per-file failures (corrupt
JSON, schema mismatch, OS read errors) with a ``fix_hint``. The indexer
writes rows there from two paths:

1. File-level exceptions in the ``run_index`` orchestrator (when
   ``ingestor.extract`` raises).
2. Per-line / per-record warnings the ingestor reports via the
   ``error_sink`` callback (bad JSON line, non-object record, etc.).

Both paths route through ``_record_ingest_error`` below so the
table format and timestamp source are consistent.

Multiprocessing model
---------------------
Workers receive ``(file_path, db_path: str)`` tuples — NEVER live connection
objects. Each worker opens its own sqlite connection on startup (sqlite3
connections cannot be safely shared across process boundaries; pickling is
unsupported and fork-sharing corrupts the WAL).

The parent process holds the ``WriteLock`` so concurrent ``recall index``
invocations fail fast rather than corrupting the WAL. Workers do NOT take
the lock — they trust the parent's hold.

A tiny ``Ingestor`` Protocol decouples the indexer from any specific source
format. Real ingestors (claude_code, claude_ai, markdown, etc.) live in
``core/ingest/`` and are written by a sibling agent.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Protocol, runtime_checkable

from aurochs_recall.core.db import connect
from aurochs_recall.core.locks import WriteLock
from aurochs_recall.core.recovery import verify_or_rebuild
from aurochs_recall.core.types import Drawer

# Batch size for INSERT transactions. Tuned for "a few seconds of work
# per commit on commodity hardware" — large enough to amortize fsync,
# small enough that an interrupted run loses bounded work.
_BATCH_SIZE: int = 1000

# Lock timeout for the parent's WriteLock. Long enough to wait through a
# legitimate concurrent indexer; short enough to fail in human time if
# something is wedged.
_WRITE_LOCK_TIMEOUT: float = 120.0


# ---------------------------------------------------------------------------
# Ingestor protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class Ingestor(Protocol):
    """A drawer source.

    Implementations live in ``core/ingest/<name>.py`` and should be safe to
    instantiate cheaply — the indexer constructs one per source per run.

    The two methods serve different purposes:

    * ``discover_files`` — yields paths the indexer should hand to workers.
      Used to decide what's incrementally stale (mtime check) and what to
      fan out across the worker pool.
    * ``read_drawers`` — given a single file path, yields ``Drawer``
      instances. Workers call this; never the parent.
    """

    name: str

    def discover_files(self, root: Path) -> Iterator[Path]:
        """Yield candidate file paths under ``root``."""
        ...

    def read_drawers(self, file_path: Path) -> Iterator[Drawer]:
        """Yield drawers from a single source file."""
        ...


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------

class Indexer:
    """Stateful indexer bound to a database path.

    Construct once per ``recall index`` invocation; the constructor runs
    integrity verification and acquires no lock until ``index_*`` is called.
    """

    def __init__(self, db_path: Path | str) -> None:
        """Open the database, verify integrity, prepare for indexing.

        Per plan v4: ``verify_or_rebuild`` runs on every Indexer init so a
        fresh process catches WAL-recovery-needed cases before hitting the
        write path.
        """
        self.db_path: Path = Path(db_path)
        verify_or_rebuild(self.db_path)

    # ----- top-level entry points -----------------------------------------

    def index_drawers(self, drawers: Iterable[Drawer]) -> int:
        """Insert a stream of drawers, deduping by (content_hash, source, source_id).

        Returns the number of new rows actually inserted (not counting
        existing duplicates). Holds the ``WriteLock`` for the entire call.
        """
        added = 0
        with WriteLock(self.db_path, timeout=_WRITE_LOCK_TIMEOUT):
            conn = connect(self.db_path)
            try:
                added = _bulk_insert_drawers(conn, drawers)
            finally:
                conn.close()
        return added

    def index_source(self, source_name: str, ingestor: Ingestor, root: Path) -> int:
        """Walk an ingestor's discovered files and index every drawer.

        For T0 this is single-process — the multiprocessing pool is wired
        via :py:meth:`index_source_parallel`. The single-process path is
        canonical for fixtures + tests; the parallel path is the production
        codepath.

        Returns total drawers added across all files.
        """
        added = 0
        with WriteLock(self.db_path, timeout=_WRITE_LOCK_TIMEOUT):
            conn = connect(self.db_path)
            try:
                for file_path in ingestor.discover_files(root):
                    if not _file_needs_index(conn, source_name, file_path):
                        continue
                    drawers = list(ingestor.read_drawers(file_path))
                    if not drawers:
                        _record_index_state(conn, source_name, file_path, 0)
                        continue
                    inserted = _bulk_insert_drawers(conn, drawers)
                    _record_index_state(conn, source_name, file_path, inserted)
                    added += inserted
            finally:
                conn.close()
        return added

    def index_source_parallel(
        self,
        source_name: str,
        ingestor: Ingestor,
        root: Path,
        *,
        workers: int | None = None,
    ) -> int:
        """Like :py:meth:`index_source` but fans out across a process pool.

        Workers receive ``(file_path, db_path)`` tuples — never live
        connection objects. Each worker opens its own connection and the
        parent owns the ``WriteLock`` for the entire run.

        IMPORTANT: callers must guard their entry point with
        ``if __name__ == "__main__":`` so spawned workers don't re-import
        and fork-bomb on Windows.
        """
        # Lazy import so single-process callers don't pay the cost.
        import multiprocessing as mp

        worker_count = workers or max(1, (os.cpu_count() or 2) // 2)
        files = list(ingestor.discover_files(root))
        if not files:
            return 0

        # Filter to files that actually need indexing; do this in the
        # parent so we don't fork workers just to no-op.
        with WriteLock(self.db_path, timeout=_WRITE_LOCK_TIMEOUT):
            conn = connect(self.db_path)
            try:
                stale = [f for f in files if _file_needs_index(conn, source_name, f)]
            finally:
                conn.close()

            if not stale:
                return 0

            payload = [(str(f), str(self.db_path), source_name) for f in stale]
            with mp.Pool(processes=worker_count) as pool:
                # Import the worker function lazily — see _index_one_file.
                results = pool.starmap(_worker_index_one_file, payload)
        return sum(results)


# ---------------------------------------------------------------------------
# Worker entry point (must be top-level so it pickles)
# ---------------------------------------------------------------------------

def _worker_index_one_file(file_path_str: str, db_path_str: str, source_name: str) -> int:
    """Worker: open own connection, index one file, return count.

    Exists at module top-level (not nested inside Indexer) so pickling
    works on both fork and spawn start methods.
    """
    # Import lazily to avoid heavy imports at fork time.
    from importlib import import_module

    file_path = Path(file_path_str)
    db_path = Path(db_path_str)

    # Worker discovers its own ingestor by source name. T0 ships only the
    # markdown ingestor wired through; future ingestors register here.
    ingestor_module = import_module(f"aurochs_recall.core.ingest.{source_name}")
    ingestor = ingestor_module.Ingestor()

    drawers = list(ingestor.read_drawers(file_path))
    if not drawers:
        return 0

    conn = connect(db_path)
    try:
        inserted = _bulk_insert_drawers(conn, drawers)
        _record_index_state(conn, source_name, file_path, inserted)
        return inserted
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _file_needs_index(
    conn: sqlite3.Connection,
    source_name: str,
    file_path: Path,
) -> bool:
    """Return True if mtime exceeds the recorded last_indexed_mtime.

    Per plan v4 / v5 the watermark is per-file, not per-source — a worker
    finishing file N+5 doesn't advance past worker B's still-in-flight file
    N. A missing index_state row means "never indexed" and returns True.
    """
    try:
        mtime = int(file_path.stat().st_mtime)
    except OSError:
        # File vanished between discovery and check — skip it.
        return False

    row = conn.execute(
        "SELECT last_indexed_mtime FROM index_state "
        "WHERE source = ? AND source_path = ?",
        (source_name, str(file_path)),
    ).fetchone()
    if row is None:
        return True
    return mtime > row[0]


def _record_index_state(
    conn: sqlite3.Connection,
    source_name: str,
    file_path: Path,
    drawer_count: int,
) -> None:
    """Upsert the per-file index_state row to the file's current mtime."""
    try:
        stat = file_path.stat()
    except OSError:
        return
    conn.execute(
        "INSERT INTO index_state (source, source_path, last_indexed_mtime, "
        "last_indexed_size, drawer_count) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(source, source_path) DO UPDATE SET "
        "last_indexed_mtime = excluded.last_indexed_mtime, "
        "last_indexed_size = excluded.last_indexed_size, "
        "drawer_count = drawer_count + excluded.drawer_count",
        (source_name, str(file_path), int(stat.st_mtime), stat.st_size, drawer_count),
    )


def _record_ingest_error(
    conn: sqlite3.Connection,
    *,
    source: str,
    source_path: str | None,
    reason: str,
    fix_hint: str | None = None,
) -> None:
    """Insert a row into ``ingest_errors``.

    Schema (from 0001_initial.sql)::

        CREATE TABLE ingest_errors (
          id INTEGER PRIMARY KEY,
          source TEXT NOT NULL,
          source_path TEXT,
          reason TEXT NOT NULL,
          fix_hint TEXT,
          occurred_at INTEGER NOT NULL,
          retry_count INTEGER NOT NULL DEFAULT 0
        );

    Errors are append-only; ``recall errors`` reads them back and
    formats the latest N for the user. A best-effort write — if the
    table doesn't exist on a pre-baseline DB we swallow the
    OperationalError so the indexer keeps running.
    """
    try:
        conn.execute(
            "INSERT INTO ingest_errors (source, source_path, reason, fix_hint, "
            "occurred_at, retry_count) VALUES (?, ?, ?, ?, ?, 0)",
            (source, source_path, reason, fix_hint, _now()),
        )
    except sqlite3.OperationalError:
        # Pre-baseline DB without the ingest_errors table: don't crash.
        pass


def _suggest_fix_hint(reason: str) -> str | None:
    """Heuristic fix-hint generator from a raw exception/warning string.

    Maps common failure patterns to actionable hints. Returns None when
    we don't have a useful suggestion — better to show no hint than a
    misleading one.
    """
    lower = reason.lower()
    if "bad jsonl" in lower or "jsondecodeerror" in lower:
        return (
            "One or more JSONL lines are malformed. Open the file at the "
            "reported line number and check for truncation or stray bytes."
        )
    if "non-object jsonl" in lower or "non-object message" in lower or "non-object conversation" in lower:
        return (
            "Record is not a JSON object. The export may be from a different "
            "schema version; rerun the export or skip the offending record."
        )
    if "no message array" in lower:
        return (
            "Conversation has neither 'chat_messages' nor 'messages'. The "
            "export may be partial; re-export from claude.ai."
        )
    if "without uuid" in lower:
        return (
            "Conversation lacks a uuid/id. Likely an interrupted export — "
            "re-export the data."
        )
    if "invalid json" in lower:
        return (
            "Top-level JSON is invalid. The file may be truncated; re-export "
            "or trim trailing partial-write bytes."
        )
    if "cannot determine session uuid" in lower:
        return (
            "JSONL filename and parent directory are not UUIDs. Move the file "
            "into a directory whose name is a session UUID, or rename the file."
        )
    if "failed to read" in lower or "permissionerror" in lower:
        return (
            "Filesystem refused the read. Check file permissions and that "
            "the path is accessible from this user."
        )
    if "expected top-level json array" in lower:
        return (
            "claude_ai exports must be a top-level JSON array. The file may "
            "be a single conversation; wrap it in [] or re-export."
        )
    return None


def _bulk_insert_drawers(conn: sqlite3.Connection, drawers: Iterable[Drawer]) -> int:
    """Insert drawers in batches of ``_BATCH_SIZE``, deduping silently.

    Returns the number of rows actually inserted. Uses ``INSERT OR IGNORE``
    against the (content_hash, source, source_id) UNIQUE index so re-runs
    don't double-index. The FTS5 virtual table is kept in sync via the
    ``drawer_meta`` rowid binding.
    """
    inserted = 0
    batch: list[Drawer] = []

    def flush() -> int:
        if not batch:
            return 0
        pre_count = conn.execute(
            "SELECT COUNT(*) FROM drawer_meta"
        ).fetchone()[0]

        conn.execute("BEGIN")
        try:
            for d in batch:
                conn.execute(
                    "INSERT OR IGNORE INTO drawer_meta ("
                    "drawer_uid, source, source_id, source_path, role, register, "
                    "thread_id, parent_uid, position_in_thread, branch_count, "
                    "created_at, content_hash, risk_score, risk_score_version, "
                    "hash_input_version) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        d.drawer_uid,
                        d.source,
                        d.source_id,
                        d.source_path,
                        d.role,
                        d.register,
                        d.thread_id,
                        d.parent_uid,
                        d.position_in_thread,
                        d.branch_count,
                        d.created_at,
                        d.content_hash,
                        d.risk_score,
                        d.risk_score_version,
                        d.hash_input_version,
                    ),
                )
                # Mirror into FTS5. The dedup branch above means we may
                # skip writes on duplicate rows; only insert into FTS5 if
                # the meta row is new.
                if conn.total_changes:
                    rowid = conn.execute(
                        "SELECT rowid FROM drawer_meta WHERE drawer_uid = ?",
                        (d.drawer_uid,),
                    ).fetchone()
                    if rowid is not None:
                        conn.execute(
                            "INSERT OR IGNORE INTO drawers_fts(rowid, content) "
                            "VALUES (?, ?)",
                            (rowid[0], d.content),
                        )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise

        post_count = conn.execute(
            "SELECT COUNT(*) FROM drawer_meta"
        ).fetchone()[0]
        result = post_count - pre_count
        batch.clear()
        return result

    for drawer in drawers:
        batch.append(drawer)
        if len(batch) >= _BATCH_SIZE:
            inserted += flush()

    inserted += flush()
    return inserted


def _now() -> int:
    """Return the current epoch second as an int."""
    return int(time.time())


# ---------------------------------------------------------------------------
# Orchestrator: run_index
# ---------------------------------------------------------------------------
# This is the entry point the CLI calls. It bridges between the
# sources_config layer and the actual on-disk ingestors. The
# ``Indexer`` class above is a lower-level building block; ``run_index``
# is the top-level "do everything" routine.
#
# Design note: ``Indexer.index_source()`` was sketched by the spine
# agent against a protocol shape (``discover_files`` / ``read_drawers``)
# that the ingestors don't implement. Rather than refactor either side,
# this orchestrator uses the real ingestor protocol from
# ``core/ingest/_base.py`` (``can_handle`` / ``extract``) and the
# already-tested ``_bulk_insert_drawers`` / ``_file_needs_index`` /
# ``_record_index_state`` helpers above.


_INGESTOR_REGISTRY: dict[str, str] = {
    "claude_code": "aurochs_recall.core.ingest.claude_code:ClaudeCodeIngestor",
    "claude_ai":   "aurochs_recall.core.ingest.claude_ai:ClaudeAiIngestor",
    "markdown":    "aurochs_recall.core.ingest.markdown:MarkdownIngestor",
    # 'chatgpt' / 'capture' deferred to later patches per plan v5.
}


def _resolve_ingestor(type_name: str):
    """Import + instantiate the ingestor for a given source-type string."""
    target = _INGESTOR_REGISTRY.get(type_name)
    if target is None:
        raise ValueError(
            f"Unknown source type {type_name!r}. "
            f"Supported: {sorted(_INGESTOR_REGISTRY)}"
        )
    module_name, _, class_name = target.partition(":")
    from importlib import import_module

    module = import_module(module_name)
    cls = getattr(module, class_name)
    return cls()


def _walk_source_files(root: Path, ingestor, source=None) -> Iterator[Path]:
    """Yield candidate files under ``root`` that ``ingestor`` accepts.

    Handles single-file source paths (e.g. claude_ai's
    ``conversations.json``) by yielding just that file. Directories are
    walked with ``rglob('*')`` and filtered through ``can_handle``,
    then through the ``source.include`` / ``source.exclude`` glob
    patterns from sources.toml (B3 fix).

    ``source`` is the ``SourceEntry`` (or None for legacy callers).
    When provided, glob patterns are applied as follows:

    * ``exclude`` (deny-list): any matching path is skipped. Patterns
      are evaluated against the absolute path AND the path relative
      to ``root`` so authors can write either ``"**/.git/**"`` or
      ``".git/**"``.
    * ``include`` (allow-list): if non-empty, only paths matching
      one of the patterns pass. If empty/None, all paths pass.
      Exclude takes precedence over include.

    Patterns are matched via ``fnmatch.fnmatch`` against three
    candidate strings (absolute posix path, root-relative posix
    path, bare filename) so both ``**/node_modules/**`` and
    ``node_modules/**`` and ``*.tmp`` work as authors expect.
    """
    if root.is_file():
        if ingestor.can_handle(root):
            if _path_passes_filters(root, root.parent, source):
                yield root
        return
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if not (path.is_file() and ingestor.can_handle(path)):
            continue
        if not _path_passes_filters(path, root, source):
            continue
        yield path


def _path_passes_filters(path: Path, root: Path, source) -> bool:
    """Apply ``source.exclude`` (deny) and ``source.include`` (allow) globs.

    Returns True iff the path should be processed. ``source`` may be
    None (no filters) or have ``.include`` / ``.exclude`` tuples.

    Patterns are matched against:

    1. The absolute path (so ``**/node_modules/**`` works).
    2. The path relative to ``root`` (so ``node_modules/**`` works).
    3. The bare file name (so ``*.tmp`` works).

    Any of those matching counts as a hit. Exclude takes precedence
    over include — a path that matches both is dropped.
    """
    if source is None:
        return True
    excludes = getattr(source, "exclude", ()) or ()
    includes = getattr(source, "include", ()) or ()
    if not excludes and not includes:
        return True

    import fnmatch

    # Build the candidate strings the glob is matched against.
    posix_abs = path.as_posix()
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = posix_abs
    name = path.name

    def _matches(pattern: str) -> bool:
        # fnmatch is the simplest cross-platform glob; ** works as a
        # plain wildcard against the joined posix path.
        return (
            fnmatch.fnmatch(posix_abs, pattern)
            or fnmatch.fnmatch(rel, pattern)
            or fnmatch.fnmatch(name, pattern)
        )

    # Exclude: deny if any pattern matches.
    for pattern in excludes:
        if _matches(pattern):
            return False

    # Include: if non-empty, require at least one match.
    if includes:
        for pattern in includes:
            if _matches(pattern):
                return True
        return False

    return True


def _extract_with_sink(ingestor, file_path: Path, sink) -> Iterator[Drawer]:
    """Call ``ingestor.extract``; pass the error_sink kwarg if supported.

    Older ingestors only define ``extract(self, path)``. Newer ones
    accept ``extract(self, path, *, error_sink=None)`` and report
    per-line warnings via the callback. We dispatch the right shape
    by introspecting the call signature.
    """
    import inspect

    try:
        sig = inspect.signature(ingestor.extract)
    except (TypeError, ValueError):
        sig = None

    accepts_sink = sig is not None and "error_sink" in sig.parameters
    if accepts_sink:
        yield from ingestor.extract(file_path, error_sink=sink)
    else:
        yield from ingestor.extract(file_path)


def run_index(
    *,
    config_path: Path | str | None = None,
    db_path: Path | str | None = None,
    quick: bool = False,
) -> int:
    """Top-level indexer invoked by ``recall index``.

    Returns 0 on success. Walks every enabled source from sources.toml,
    invokes the appropriate ingestor on each candidate file, and inserts
    drawers into ``recall.db`` (creating the schema first if absent).

    Parameters
    ----------
    config_path:
        Override sources.toml location. Falls through to
        ``load_sources_config``'s discovery order if None.
    db_path:
        Override the database path. Wins over ``[database].path`` in
        the config when set.
    quick:
        Incremental mode — skip files whose mtime hasn't changed since
        last index. The default is to walk everything (still cheap because
        the UNIQUE index makes re-inserts no-ops).
    """
    # Lazy import: keeps test fixtures that touch core.index but never
    # call run_index from paying for the heavier deps.
    from aurochs_recall.core.migrations.runner import run_migrations
    from aurochs_recall.core.sources_config import (
        SourcesConfig,
        default_database_path,
        load_sources_config,
    )

    cfg: SourcesConfig = load_sources_config(config_path)

    # --db override takes precedence over the config's [database].path.
    target_db = (
        Path(db_path).expanduser().resolve()
        if db_path is not None
        else cfg.database_path
    )
    target_db.parent.mkdir(parents=True, exist_ok=True)

    # Ensure schema is up-to-date before any insert.
    run_migrations(target_db)

    enabled = cfg.enabled_sources
    if not enabled:
        print("recall index: no enabled sources in sources.toml.")
        return 0

    total_inserted = 0
    total_skipped = 0
    print(f"Indexing into {target_db}")
    with WriteLock(target_db, timeout=_WRITE_LOCK_TIMEOUT):
        conn = connect(target_db)
        try:
            for source in enabled:
                root = source.expanded_path
                ingestor = _resolve_ingestor(source.type)
                added_for_source = 0
                files_for_source = 0
                files_skipped = 0
                # Per-source error sink: ingestors call this on per-line
                # warnings (bad JSONL, non-object records, etc.). We
                # close over `source.name` so the sink is bound to this
                # iteration without leaking into the next source.
                def _sink(*, file_path: Path, reason: str, source_name: str = source.name) -> None:
                    _record_ingest_error(
                        conn,
                        source=source_name,
                        source_path=str(file_path),
                        reason=reason,
                        fix_hint=_suggest_fix_hint(reason),
                    )

                for file_path in _walk_source_files(root, ingestor, source):
                    if quick and not _file_needs_index(
                        conn, source.name, file_path
                    ):
                        files_skipped += 1
                        continue
                    files_for_source += 1
                    try:
                        drawers = list(_extract_with_sink(ingestor, file_path, _sink))
                    except Exception as e:  # ingest-level failure
                        reason = f"{type(e).__name__}: {e}"
                        print(
                            f"  ! {source.name}: skipped {file_path} ({e})"
                        )
                        _record_ingest_error(
                            conn,
                            source=source.name,
                            source_path=str(file_path),
                            reason=reason,
                            fix_hint=_suggest_fix_hint(reason),
                        )
                        continue
                    inserted = _bulk_insert_drawers(conn, drawers)
                    _record_index_state(conn, source.name, file_path, inserted)
                    added_for_source += inserted
                total_inserted += added_for_source
                total_skipped += files_skipped
                print(
                    f"  + {source.name:<24}"
                    f" files={files_for_source:>4}"
                    f" skipped={files_skipped:>4}"
                    f" drawers+={added_for_source}"
                )
        finally:
            conn.close()

    print(f"Indexed {total_inserted} new drawer(s); {total_skipped} file(s) skipped.")
    return 0


__all__ = [
    "Ingestor",
    "Indexer",
    "run_index",
]
