# Migrations

aurochs-recall manages its SQLite schema through a runner that takes
migration safety seriously. The full design is in
[plan v4 / v5](https://github.com/skovalik/aurochs-recall) — this page
covers the user-facing operational surface.

## How it runs

- Migrations live in `core/migrations/versions/<NNNN>_<slug>.sql`.
- `schema_version` table tracks the highest applied version + a `status`
  field (`applied` / `failed` / `in_progress`).
- The runner takes `BEGIN EXCLUSIVE` plus an OS-level advisory lockfile
  (`recall.db.migrate.lock`).
- Migrations are sequentially enforced — version 3 cannot run unless
  version 2 is `applied`.
- Pre-v0.1 baseline detection runs once, on first open after upgrade,
  to mark legacy databases at the appropriate version.

## When migrations run

- On every `recall` invocation that needs the database.
- The runner is idempotent: if no new migrations apply, it returns
  immediately.
- If a migration is `in_progress` (lockfile present, holder PID alive),
  the operation blocks with a clear message. Use `recall lock --status`
  to inspect.
- If the runner detects a stale lockfile (PID dead per
  `psutil.pid_exists`), it logs an audit entry and force-releases.

## Failure handling

Single-statement-per-transaction means a failed statement leaves the
schema at the previous version, not in a half-applied state. The
runner records `status = 'failed'` and surfaces the error.

You can re-run with `recall ingest --migrate` to retry, or roll back
with `recall verify --rebuild-fts` for FTS-rebuild migrations
specifically.

## FTS5-rebuild migrations

When a migration rebuilds `drawers_fts`, `risk_audit`, `hidden_drawers`,
and `access_log` are joined back via the stable `drawer_uid`. This is
the key reason `drawer_uid` exists rather than relying on FTS5 `rowid`
— rebuilds change rowids; `drawer_uid` doesn't move.

## TODO

Migration list, version history, and rollback notes get filled in
during build.
