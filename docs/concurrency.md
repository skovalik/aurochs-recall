# Concurrency

aurochs-recall is single-writer, multi-reader. Concurrency primitives
are explicit, not implicit.

## Primitives

- **OS-level write lockfile** — `recall.db.write.lock` next to the
  database. ANY writer (CLI ingest, CLI extract, MCP server when
  writing access_log entries) acquires this before opening a write
  connection.
- **OS-level migration lockfile** — `recall.db.migrate.lock`. Held only
  by the migration runner. Coexists with read traffic but blocks
  writes.
- **`PRAGMA busy_timeout = 30000`** — readers wait up to 30s on
  contention before giving up.
- **`PRAGMA journal_mode = WAL`** — concurrent reads do not block on
  writers.
- **`PRAGMA wal_autocheckpoint = 1000`** — bounded WAL growth.
- **SIGINT handler** — graceful checkpoint on Ctrl-C; writers don't
  leave half-applied state.

## Windows lockfile semantics

On Windows the lockfile FD is opened with `close_fds=True` and a
non-inheritable handle flag, so child processes don't accidentally
extend the lock's lifetime. The lockfile records the holder PID; on
startup a fresh process checks `psutil.pid_exists(holder_pid)`. If the
holder is dead the lockfile is force-released with an audit log entry.

## What `recall status` shows

```
recall status
```

reports:

- `schema_version`
- `drawer_count`
- `wal_size_pages` — useful for debugging autocheckpoint behavior under
  high MCP burst
- `lockfile_state` — held / stale / free
- `lockfile_holder_pid` — if held, who

## MCP schema-version polling

The MCP server polls `schema_version` periodically (every N tool calls)
and recycles its connection if the version changes mid-session. This
prevents stale-result hazards after a migration.

## Async access_log queue

Access log writes don't block the read path. They go through an
in-process queue and are flushed in batches. If the queue is full
(memory pressure, paranoid setting), the searcher drops the oldest
unflushed entry rather than block — meta-memory is best-effort.

## What goes wrong, and what doesn't

- **Two CLI ingests at once** — second one blocks on the lockfile,
  waits up to `--lock-timeout` (default 60s), then errors with a clear
  message and exit code 3.
- **CLI ingest + MCP read** — fine. Readers don't block writers in WAL
  mode.
- **Migration runner running** — blocks new writers; readers proceed
  if the migration is non-FTS-rebuild, otherwise readers may see
  transient inconsistency until the migration commits.
- **Crashed writer leaves stale lockfile** — next startup detects via
  `psutil.pid_exists`, force-releases.

## TODO

Stress test under high MCP burst once the MCP server lands.
