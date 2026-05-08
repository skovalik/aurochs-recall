# Backup & restore

`recall backup` and `recall restore` are first-class CLI commands. The
memory architecture has a single point of failure — the SQLite database
— and the CLI takes that seriously.

## What gets backed up

A `recall backup` snapshot includes:

- `drawer_meta` and the `drawers_fts` virtual table contents
- `entities`, `aliases`, `entity_types`, `predicates`, `relationships`
- `taxonomy_audit` — taxonomy evolution history
- `access_log` — meta-memory, retrieval pattern data
- `risk_audit` — content-safety scanner output history
- `extraction_runs` — versioned extraction provenance
- `schema_version` — for round-trip integrity

## Usage

```bash
# Snapshot to a directory; filename includes UTC timestamp
recall backup ~/recall-backups/

# Custom path
recall backup ~/recall-backups/before-big-ingest.db

# With [backup] extra: zstandard compression
recall backup --compress ~/recall-backups/
```

## Restore

```bash
recall restore ~/recall-backups/2026-05-07T14-32-00.db.zst
recall restore ~/recall-backups/2026-05-07T14-32-00.db
```

Restore is **destructive** — it overwrites the active database. The
runner refuses to restore on top of a live writer (lockfile check) and
prompts for confirmation if the active database is non-empty. Use
`--force` to skip the prompt; back up first.

## Verify

```bash
recall verify
recall verify ~/recall-backups/some-snapshot.db
```

`verify` runs:

- `PRAGMA integrity_check`
- FK enforcement check (every connection has `foreign_keys=ON`; verify
  asserts no orphans)
- FTS5 rowid alignment (`drawers_fts.rowid == drawer_meta.rowid` for
  every alive drawer)
- `content_hash` re-computation on a sampled subset

## Recommended cadence

- **After any non-trivial extract run** — extraction is the most
  expensive operation; back up before and after.
- **Before any migration** — the runner takes `BEGIN EXCLUSIVE` and
  rolls back on failure, but a backup is cheap insurance.
- **Periodic** — at whatever cadence your existing backup system
  covers your home directory.

## TODO

Backup-restore round-trip tests, compression-ratio measurements, and
encrypted-backup design are build-time items.
