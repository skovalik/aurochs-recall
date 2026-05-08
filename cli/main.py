"""recall CLI — argparse-based, subcommand-dispatched.

T0 surface (built):
  recall init             interactive discovery wizard, writes sources.toml
  recall index            run the indexer (--quick / --bg flags)
  recall search "QUERY"   bm25 search; flags: --source, --since, --until,
                          --register, --role, --limit, --full, --json,
                          --raw, --open
  recall <bare query>     shorthand for `recall search`
  recall status           DB stats (drawer count per source, db file size,
                          last_indexed_at, schema_version, wal_size_pages)
  recall errors           per-file ingest errors with fix hints
  recall migrate          apply pending migrations (--status / --baseline-from-existing)
  recall verify           FK + drawer_uid integrity (--deep adds content_hash)
  recall types list       list seed entity types
  recall graph entity N   basic entity lookup; show linked drawers via citations

The implementations are deliberately thin: each handler imports its
dependency at call time so a fast `recall --help` doesn't trigger heavy
imports (sqlite, tomllib, etc.).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence


PROG = "recall"
EPILOG = (
    "Default subcommand is `search` — `recall \"QUERY\"` is equivalent to "
    "`recall search \"QUERY\"`."
)


# ----------------------------------------------------------------------
# Top-level entrypoint
# ----------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Parse argv and dispatch. Returns the process exit code."""
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)

    # Bare-query shorthand: insert `search` before the first positional that
    # isn't a known subcommand. This handles both `recall mehrwerk` and
    # `recall --db PATH mehrwerk` correctly. Global flags are recognized so
    # we skip past them rather than treating them as the query.
    argv = _maybe_insert_search_subcommand(argv)

    parser = build_parser()
    args = parser.parse_args(argv)

    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help()
        return 0

    try:
        return int(handler(args) or 0)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except FileNotFoundError as e:
        print(f"recall: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # last-resort guard so users see a one-liner
        if os.environ.get("RECALL_DEBUG"):
            raise
        print(f"recall: error: {e}", file=sys.stderr)
        return 1


# ----------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Memory architecture for your AI conversations.",
        epilog=EPILOG,
    )
    p.add_argument("--config", help="Path to sources.toml (overrides discovery).")
    p.add_argument("--db", help="Path to recall.db (overrides config).")
    p.add_argument(
        "--version",
        action="version",
        version=_version_string(),
    )
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    _add_init(sub)
    _add_index(sub)
    _add_search(sub)
    _add_status(sub)
    _add_errors(sub)
    _add_migrate(sub)
    _add_verify(sub)
    _add_types(sub)
    _add_graph(sub)

    return p


def _version_string() -> str:
    try:
        from core import __version__  # type: ignore
    except Exception:
        return f"{PROG} (unknown version)"
    return f"{PROG} {__version__}"


_SUBCOMMANDS = frozenset(
    {"init", "index", "search", "status", "errors", "migrate", "verify", "types", "graph"}
)

# Global flags that take a value (advance index by 2). Boolean global flags
# (no value) advance by 1; we treat anything starting with `-` that isn't in
# the value-taking set as boolean.
_GLOBAL_VALUE_FLAGS = frozenset({"--config", "--db"})


def _maybe_insert_search_subcommand(argv: list[str]) -> list[str]:
    """If argv contains no subcommand, inject `search` so a bare query is
    routed to the search handler. Skips past global flags so
    `recall --db PATH "query"` works the same as `recall "query"`.
    """
    if not argv:
        return argv

    i = 0
    while i < len(argv):
        token = argv[i]
        if token in _SUBCOMMANDS:
            return argv  # already has a subcommand
        if token in {"-h", "--help", "--version"}:
            return argv  # let argparse handle
        if token.startswith("-"):
            # Global flag — advance past it (and its value if any).
            if "=" in token:
                i += 1
                continue
            if token in _GLOBAL_VALUE_FLAGS:
                i += 2
                continue
            i += 1
            continue
        # First non-flag, non-subcommand token: treat as query, inject `search`.
        return [*argv[:i], "search", *argv[i:]]
    return argv


# ----------------------------------------------------------------------
# Subcommand: init
# ----------------------------------------------------------------------


def _add_init(sub: Any) -> None:
    p = sub.add_parser("init", help="Discovery wizard: detect sources, write sources.toml.")
    p.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip prompts; auto-include all detected sources except CLAUDE.md.",
    )
    p.add_argument(
        "--out",
        help="Where to write sources.toml. Default: per-user config dir.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing sources.toml without prompting.",
    )
    p.set_defaults(_handler=_cmd_init)


def _cmd_init(args: argparse.Namespace) -> int:
    from core.sources_config import (
        default_config_path,
        default_database_path,
        detect_candidate_sources,
        render_starter_toml,
    )

    out_path = Path(args.out).expanduser() if args.out else default_config_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if out_path.exists() and not args.force:
        if args.non_interactive:
            print(
                f"sources.toml already exists at {out_path}. Use --force to overwrite.",
                file=sys.stderr,
            )
            return 1
        ans = input(f"sources.toml exists at {out_path}. Overwrite? [y/N] ").strip().lower()
        if ans not in {"y", "yes"}:
            print("Aborted; existing sources.toml left in place.")
            return 0

    print(f"Discovering sources... (writing to {out_path})")
    candidates = detect_candidate_sources()

    if not candidates:
        print("  (no candidates detected; you'll add sources by hand)")

    selected: list[dict] = []
    for c in candidates:
        if args.non_interactive:
            # Default: skip CLAUDE.md (paths containing 'CLAUDE.md'); include the rest.
            if "CLAUDE.md" in c["path"]:
                continue
            selected.append(c)
            print(f"  + {c['name']:30}  {c['path']}  ({c['hint']})")
            continue

        marker = "?" if "CLAUDE.md" in c["path"] else ">"
        prompt = f"  {marker} {c['name']:30}  {c['path']}\n      {c['hint']} — include? [Y/n] "
        ans = input(prompt).strip().lower()
        if ans in {"", "y", "yes"}:
            selected.append(c)

    db_path = default_database_path()
    starter = render_starter_toml(database_path=db_path, sources=selected)
    out_path.write_text(starter, encoding="utf-8")

    print()
    print(f"Wrote {out_path} with {len(selected)} source entries.")
    if _has_non_english(candidates):
        print(
            "  Note: some detected paths contain non-English content. "
            "Install with `pip install aurochs-recall[multilingual]` "
            "for multilingual scoring."
        )
    print()
    print(f"Next: `recall index` to build the index at {db_path}.")
    return 0


def _has_non_english(candidates: list[dict]) -> bool:
    """Heuristic: any path with non-ASCII characters likely indexes non-English content."""
    for c in candidates:
        if any(ord(ch) > 127 for ch in c["path"]):
            return True
    return False


# ----------------------------------------------------------------------
# Subcommand: index
# ----------------------------------------------------------------------


def _add_index(sub: Any) -> None:
    p = sub.add_parser("index", help="Build or refresh the index.")
    p.add_argument(
        "--quick",
        action="store_true",
        help="Incremental: skip files unchanged since last index (mtime+size).",
    )
    p.add_argument(
        "--bg",
        action="store_true",
        help="Run in background (daemonize). Returns immediately.",
    )
    p.set_defaults(_handler=_cmd_index)


def _cmd_index(args: argparse.Namespace) -> int:
    # The orchestrator lives in core.index alongside the lower-level
    # Indexer class. Keeping the import lazy means single-process tests
    # that only touch other modules don't pay the import cost.
    try:
        from core.index import run_index
    except ImportError:
        print(
            "recall index: indexer module not yet available in this build.",
            file=sys.stderr,
        )
        return 3

    if args.bg:
        # Spawn the same command without --bg, detached. Cross-platform
        # daemonization is non-trivial; for T0 we use subprocess.Popen with
        # the platform-appropriate flags.
        cmd = [sys.executable, "-m", "cli.main", "index"]
        if args.quick:
            cmd.append("--quick")
        if sys.platform == "win32":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            subprocess.Popen(
                cmd,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        else:
            subprocess.Popen(cmd, start_new_session=True, close_fds=True)
        print("Indexing started in background. Check progress: `recall status`.")
        return 0

    config_path = Path(args.config).expanduser() if args.config else None
    db_override = Path(args.db).expanduser() if args.db else None
    return int(run_index(config_path=config_path, db_path=db_override, quick=args.quick) or 0)


# ----------------------------------------------------------------------
# Subcommand: search
# ----------------------------------------------------------------------


def _add_search(sub: Any) -> None:
    p = sub.add_parser("search", help="Search the index (default subcommand).")
    p.add_argument("query", nargs="+", help="Query string. Quote multi-word queries.")
    p.add_argument(
        "--mode",
        choices=("bm25",),
        default="bm25",
        help="Search mode (T0: bm25 only).",
    )
    p.add_argument(
        "--source",
        action="append",
        help="Restrict to source (repeatable, e.g. --source claude_code --source claude_ai).",
    )
    p.add_argument("--since", help="Drawer created_at >= YYYY-MM-DD.")
    p.add_argument("--until", help="Drawer created_at <= YYYY-MM-DD.")
    p.add_argument("--register", help="Drawer register exact match.")
    p.add_argument("--role", help="Drawer role exact match (human / assistant / etc).")
    p.add_argument("--limit", type=int, default=10, help="Max hits to return.")
    p.add_argument("--full", action="store_true", help="No snippet truncation.")
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output.")
    p.add_argument("--raw", action="store_true", help="Pass query through as FTS5 syntax.")
    p.add_argument(
        "--open",
        dest="open_uid",
        help="Launch $EDITOR at the source path of <drawer-uid>.",
    )
    p.set_defaults(_handler=_cmd_search)


def _cmd_search(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args)

    if args.open_uid:
        return _open_drawer(db_path, args.open_uid)

    query = " ".join(args.query).strip()
    if not query:
        print("recall: empty query.", file=sys.stderr)
        return 2

    since = _parse_date(args.since) if args.since else None
    until = _parse_date(args.until, end_of_day=True) if args.until else None

    from core.search import Searcher
    from core.retriever.fts5 import FTS5QueryError

    try:
        with Searcher(db_path=db_path) as s:
            try:
                hits = s.search(
                    query,
                    mode=args.mode,
                    full=args.full,
                    source=args.source,
                    since=since,
                    until=until,
                    register=args.register,
                    role=args.role,
                    limit=args.limit,
                    raw=args.raw,
                )
            except FTS5QueryError as e:
                hint = (
                    " (use --raw only with valid FTS5 syntax; drop --raw for literal search)"
                    if args.raw
                    else ""
                )
                print(f"recall: query rejected by FTS5: {e}{hint}", file=sys.stderr)
                return 2
            drawers = list(s.last_drawers)
    except sqlite3.OperationalError as e:
        print(
            f"recall: cannot open recall.db at {db_path}: {e}\n"
            "  Run `recall init` then `recall index` first.",
            file=sys.stderr,
        )
        return 2

    if args.json:
        _print_hits_json(hits, drawers)
    else:
        _print_hits_human(hits, drawers, full=args.full)
    return 0 if hits else 1


def _print_hits_human(hits: list, drawers: list, *, full: bool) -> None:
    if not hits:
        print("No hits.")
        return
    for i, (hit, drawer) in enumerate(zip(hits, drawers), 1):
        date_str = _format_drawer_date(hit.created_at)
        print(f"{i}. [bm25={hit.score:.2f}]  {hit.drawer_uid}  ({date_str})")
        if hit.snippet:
            for line in _wrap_snippet(hit.snippet, indent="   ", full=full):
                print(line)
        if drawer.source_path:
            print(f"   {drawer.source_path}")
        print()


def _wrap_snippet(snippet: str, *, indent: str, full: bool) -> list[str]:
    """Indent snippet lines. Searcher already handled width + ANSI."""
    if full:
        return [indent + ln for ln in (snippet.splitlines() or [snippet])]
    return [indent + '"' + snippet + '"']


def _print_hits_json(hits: list, drawers: list) -> None:
    payload = []
    for hit, drawer in zip(hits, drawers):
        payload.append(
            {
                "drawer_uid": hit.drawer_uid,
                "source": hit.source,
                "source_id": drawer.source_id,
                "source_path": drawer.source_path,
                "role": drawer.role,
                "register": drawer.register,
                "thread_id": drawer.thread_id,
                "parent_uid": drawer.parent_uid,
                "position_in_thread": drawer.position_in_thread,
                "created_at": hit.created_at,
                "content_hash": drawer.content_hash,
                "score": hit.score,
                "rank": hit.rank,
                "snippet": _strip_ansi(hit.snippet),
                "content": drawer.content,
            }
        )
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _strip_ansi(s: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _open_drawer(db_path: Path, uid_or_prefix: str) -> int:
    """Resolve a drawer UID (full or prefix) and launch $EDITOR at its source path."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT drawer_uid, source_path FROM drawer_meta "
            "WHERE drawer_uid = ? OR drawer_uid LIKE ? LIMIT 5",
            (uid_or_prefix, f"%:{uid_or_prefix}%"),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print(f"recall: no drawer matched {uid_or_prefix!r}.", file=sys.stderr)
        return 2
    if len(rows) > 1:
        print("recall: prefix is ambiguous; matches:", file=sys.stderr)
        for r in rows:
            print(f"  {r['drawer_uid']}", file=sys.stderr)
        return 2
    src = rows[0]["source_path"]
    if not src:
        print(f"recall: drawer {rows[0]['drawer_uid']} has no source_path.", file=sys.stderr)
        return 2
    editor = os.environ.get("EDITOR") or ("notepad" if sys.platform == "win32" else "vi")
    cmd = shlex.split(editor) + [src]
    return subprocess.call(cmd)


# ----------------------------------------------------------------------
# Subcommand: status
# ----------------------------------------------------------------------


def _add_status(sub: Any) -> None:
    p = sub.add_parser("status", help="Show DB stats and indexer state.")
    p.set_defaults(_handler=_cmd_status)


def _cmd_status(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args)
    if not db_path.exists():
        print(f"recall.db not found at {db_path}. Run `recall init` then `recall index`.")
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        size_bytes = db_path.stat().st_size
        print(f"DB:        {db_path}  ({_humanize_bytes(size_bytes)})")

        # Schema version
        try:
            sv_row = conn.execute(
                "SELECT MAX(version) AS v, MAX(applied_at) AS at "
                "FROM schema_version WHERE status = 'applied'"
            ).fetchone()
            sv = sv_row["v"] if sv_row else None
            sv_at = sv_row["at"] if sv_row else None
            if sv is not None:
                print(f"Schema:    v{sv}  (applied {_format_drawer_date(sv_at) if sv_at else '?'})")
            else:
                print("Schema:    (no applied migrations)")
        except sqlite3.OperationalError:
            print("Schema:    (schema_version table missing — run `recall migrate`)")

        # WAL pages
        try:
            wal_row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            # PRAGMA wal_checkpoint returns (busy, log, checkpointed). We just
            # surface the log size.
            if wal_row is not None:
                print(f"WAL pages: {wal_row[1]}")
        except sqlite3.OperationalError:
            pass

        # Per-source drawer counts
        try:
            rows = conn.execute(
                "SELECT source, COUNT(*) AS n FROM drawer_meta GROUP BY source ORDER BY n DESC"
            ).fetchall()
            total = sum(r["n"] for r in rows)
            print(f"Drawers:   {total} total")
            for r in rows:
                print(f"  {r['source']:24} {r['n']}")
        except sqlite3.OperationalError as e:
            print(f"Drawers:   (drawer_meta unavailable: {e})")

        # Last indexed
        try:
            row = conn.execute(
                "SELECT MAX(last_indexed_mtime) AS at FROM index_state"
            ).fetchone()
            if row and row["at"]:
                print(f"Last index: {_format_drawer_date(int(row['at']))}")
        except sqlite3.OperationalError:
            pass

        # Errors count
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM ingest_errors").fetchone()
            n = int(row["n"]) if row else 0
            if n:
                print(f"Errors:    {n}  (run `recall errors` to see them)")
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    return 0


# ----------------------------------------------------------------------
# Subcommand: errors
# ----------------------------------------------------------------------


def _add_errors(sub: Any) -> None:
    p = sub.add_parser("errors", help="Show per-file ingest errors with fix hints.")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(_handler=_cmd_errors)


def _cmd_errors(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args)
    if not db_path.exists():
        print(f"recall.db not found at {db_path}.")
        return 1
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT source, source_path, reason, fix_hint, occurred_at, retry_count "
            "FROM ingest_errors ORDER BY occurred_at DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        print("recall: ingest_errors table missing.")
        return 1
    finally:
        conn.close()

    if args.json:
        json.dump(
            [
                {
                    "source": r["source"],
                    "source_path": r["source_path"],
                    "reason": r["reason"],
                    "fix_hint": r["fix_hint"],
                    "occurred_at": r["occurred_at"],
                    "retry_count": r["retry_count"],
                }
                for r in rows
            ],
            sys.stdout,
            ensure_ascii=False,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0

    if not rows:
        print("No ingest errors.")
        return 0

    for r in rows:
        when = _format_drawer_date(r["occurred_at"])
        print(f"[{when}] {r['source']}  retries={r['retry_count']}")
        print(f"  {r['source_path']}")
        print(f"  ! {r['reason']}")
        if r["fix_hint"]:
            print(f"  hint: {r['fix_hint']}")
        print()
    return 0


# ----------------------------------------------------------------------
# Subcommand: migrate
# ----------------------------------------------------------------------


def _add_migrate(sub: Any) -> None:
    p = sub.add_parser("migrate", help="Apply pending schema migrations.")
    p.add_argument("--status", action="store_true", help="Show status and pending; don't apply.")
    p.add_argument(
        "--baseline-from-existing",
        action="store_true",
        help="Mark current schema as v1 baseline (for pre-v0.1 dev DBs).",
    )
    p.set_defaults(_handler=_cmd_migrate)


def _cmd_migrate(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args)
    try:
        from core.migrations.runner import (  # type: ignore[attr-defined]
            apply_pending,
            list_status,
            baseline_from_existing,
        )
    except ImportError:
        print(
            "recall migrate: migration runner not yet available in this build. "
            "(Spine agent ships core/migrations/runner.py.)",
            file=sys.stderr,
        )
        return 3

    if args.status:
        info = list_status(db_path)
        print(json.dumps(info, indent=2, default=str) if isinstance(info, dict) else str(info))
        return 0
    if args.baseline_from_existing:
        baseline_from_existing(db_path)
        print(f"Marked existing schema at {db_path} as v1 baseline.")
        return 0
    n = apply_pending(db_path)
    print(f"Applied {n} migration(s).")
    return 0


# ----------------------------------------------------------------------
# Subcommand: verify
# ----------------------------------------------------------------------


def _add_verify(sub: Any) -> None:
    p = sub.add_parser("verify", help="Run integrity checks on recall.db.")
    p.add_argument("--deep", action="store_true", help="Add content_hash + FTS5 rowid checks.")
    p.set_defaults(_handler=_cmd_verify)


def _cmd_verify(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args)
    if not db_path.exists():
        print(f"recall.db not found at {db_path}.")
        return 1
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row

    issues: list[str] = []

    # Basic FK integrity (sqlite-native).
    try:
        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        for row in fk_rows:
            issues.append(
                f"FK violation: table={row[0]} rowid={row[1]} parent={row[2]} fkid={row[3]}"
            )
    except sqlite3.OperationalError as e:
        issues.append(f"FK check failed: {e}")

    # drawer_uid uniqueness + non-empty.
    try:
        bad = conn.execute(
            "SELECT COUNT(*) FROM drawer_meta WHERE LENGTH(drawer_uid) = 0"
        ).fetchone()[0]
        if bad:
            issues.append(f"{bad} drawer_meta rows have empty drawer_uid")
        dup = conn.execute(
            "SELECT drawer_uid, COUNT(*) AS n FROM drawer_meta "
            "GROUP BY drawer_uid HAVING n > 1"
        ).fetchall()
        for r in dup:
            issues.append(f"duplicate drawer_uid: {r['drawer_uid']} ({r['n']} rows)")
    except sqlite3.OperationalError as e:
        issues.append(f"drawer_meta check failed: {e}")

    if args.deep:
        # FTS5 rowid drift: drawers_fts.rowid should match drawer_meta.rowid.
        try:
            drift = conn.execute(
                "SELECT COUNT(*) FROM drawers_fts f "
                "LEFT JOIN drawer_meta m ON m.rowid = f.rowid "
                "WHERE m.rowid IS NULL"
            ).fetchone()[0]
            if drift:
                issues.append(f"{drift} FTS5 rows have no matching drawer_meta")
        except sqlite3.OperationalError as e:
            issues.append(f"FTS5 drift check failed: {e}")
        # Content-hash format (SHA-256 hex = 64 chars).
        try:
            short = conn.execute(
                "SELECT COUNT(*) FROM drawer_meta WHERE LENGTH(content_hash) != 64"
            ).fetchone()[0]
            if short:
                issues.append(f"{short} drawer_meta rows have non-SHA256 content_hash")
        except sqlite3.OperationalError as e:
            issues.append(f"content_hash format check failed: {e}")

    conn.close()

    if not issues:
        print(f"OK ({db_path})")
        return 0
    print(f"{len(issues)} issue(s) found:")
    for line in issues:
        print(f"  ! {line}")
    return 1


# ----------------------------------------------------------------------
# Subcommand: types
# ----------------------------------------------------------------------


def _add_types(sub: Any) -> None:
    p = sub.add_parser("types", help="Manage entity types (T0: list only).")
    sub2 = p.add_subparsers(dest="types_cmd")
    list_p = sub2.add_parser("list", help="List entity types.")
    list_p.set_defaults(_handler=_cmd_types_list)
    p.set_defaults(_handler=lambda args: (p.print_help() or 0))


def _cmd_types_list(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args)
    if not db_path.exists():
        print(f"recall.db not found at {db_path}.")
        return 1
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT name, description, parent_type, status, added_by FROM entity_types "
            "ORDER BY name"
        ).fetchall()
    except sqlite3.OperationalError as e:
        print(f"recall: entity_types unavailable: {e}")
        return 1
    finally:
        conn.close()
    if not rows:
        print("(no entity types)")
        return 0
    for r in rows:
        line = f"{r['name']:14} [{r['status']}/{r['added_by']}]"
        if r["parent_type"]:
            line += f"  parent={r['parent_type']}"
        if r["description"]:
            line += f"  -- {r['description']}"
        print(line)
    return 0


# ----------------------------------------------------------------------
# Subcommand: graph
# ----------------------------------------------------------------------


def _add_graph(sub: Any) -> None:
    p = sub.add_parser("graph", help="Knowledge graph queries.")
    sub2 = p.add_subparsers(dest="graph_cmd")
    ent_p = sub2.add_parser("entity", help="Look up an entity by name.")
    ent_p.add_argument("name", help="Entity name (case-insensitive).")
    ent_p.add_argument("--limit", type=int, default=20, help="Max linked drawers to show.")
    ent_p.set_defaults(_handler=_cmd_graph_entity)
    p.set_defaults(_handler=lambda args: (p.print_help() or 0))


def _cmd_graph_entity(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args)
    if not db_path.exists():
        print(f"recall.db not found at {db_path}.")
        return 1
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        ent_rows = conn.execute(
            "SELECT id, name, type FROM entities WHERE LOWER(name) = LOWER(?)",
            (args.name,),
        ).fetchall()
        if not ent_rows:
            print(f"No entity matched name {args.name!r}.")
            return 1
        for ent in ent_rows:
            print(f"# {ent['name']}  ({ent['type']})  id={ent['id']}")

            # Drawer mentions (always-on linker / extractor output).
            mentions = conn.execute(
                "SELECT m.drawer_uid, m.confidence, m.detected_by, "
                "       d.source, d.created_at "
                "FROM drawer_entity_mentions m "
                "JOIN drawer_meta d ON d.drawer_uid = m.drawer_uid "
                "WHERE m.entity_id = ? "
                "ORDER BY m.detected_at DESC, m.drawer_uid ASC "
                "LIMIT ?",
                (ent["id"], args.limit),
            ).fetchall()

            # Outgoing entity↔entity relationships (LLM-extracted edges,
            # AUTHORED_BY / USES / etc.). Empty in T0 until the extraction
            # patch lands.
            rels = conn.execute(
                "SELECT r.predicate, e2.name AS other_name, e2.type AS other_type, "
                "       r.drawer_uid, r.valid_from, r.valid_to "
                "FROM relationships r "
                "JOIN entities e2 ON e2.id = r.object_id "
                "WHERE r.subject_id = ? "
                "ORDER BY r.predicate, e2.name "
                "LIMIT ?",
                (ent["id"], args.limit),
            ).fetchall()

            if not mentions and not rels:
                print("  (no mentions or outgoing relationships)")

            if mentions:
                print(f"  ## Mentioned in {len(mentions)} drawer(s):")
                for m in mentions:
                    conf_tag = "" if m["confidence"] == 1.0 else f"  conf={m['confidence']:.2f}"
                    print(
                        f"  - {m['drawer_uid']}"
                        f"  [{m['detected_by']}{conf_tag}]"
                    )
            if rels:
                print(f"  ## Outgoing relationships:")
                for r in rels:
                    cite = f"  [{r['drawer_uid']}]" if r["drawer_uid"] else ""
                    print(
                        f"  {r['predicate']:18}-> {r['other_name']} "
                        f"({r['other_type']}){cite}"
                    )
    finally:
        conn.close()
    return 0


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _resolve_db_path(args: argparse.Namespace) -> Path:
    """Resolve the recall.db path from --db, sources.toml, or default."""
    if getattr(args, "db", None):
        return Path(args.db).expanduser().resolve()
    try:
        from core.sources_config import (
            default_database_path,
            load_sources_config,
        )

        cfg_path = Path(args.config).expanduser() if getattr(args, "config", None) else None
        cfg = load_sources_config(cfg_path)
        return cfg.database_path
    except FileNotFoundError:
        from core.sources_config import default_database_path

        return default_database_path()


def _parse_date(s: str, *, end_of_day: bool = False) -> int:
    """Parse YYYY-MM-DD into epoch seconds (UTC)."""
    try:
        d = date.fromisoformat(s)
    except ValueError as e:
        raise ValueError(f"invalid date {s!r}: expected YYYY-MM-DD") from e
    dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    if end_of_day:
        # End of day: 23:59:59.
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp())


def _format_drawer_date(epoch: int | None) -> str:
    if epoch is None:
        return "?"
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return "?"


def _humanize_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


if __name__ == "__main__":
    raise SystemExit(main())
