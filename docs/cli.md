# CLI reference

The CLI is the source of truth for aurochs-recall. The MCP server and the
Claude Code plugin both call into the same `core/` API.

## Commands

### `recall init`

Discover sources, write `sources.toml`, run the first ingest.

```bash
recall init
recall init --db-path /custom/path/recall.db
recall init --no-ingest          # write config but don't run first ingest
```

### `recall <query>`

Search. Default mode is `hybrid`.

```bash
recall "deadlock in pgbouncer"
recall "client onboarding playbook" --mode bm25
recall "voice deltas" --since 7d
recall "design tokens" --register technical --limit 20
```

Flags:

| Flag             | Default  | What                                                 |
| ---------------- | -------- | ---------------------------------------------------- |
| `--mode`         | `hybrid` | `bm25` / `hybrid` / `semantic`                       |
| `--limit`        | `10`     | Result count                                         |
| `--since`        | -        | `7d` / `30d` / ISO date                              |
| `--register`     | -        | Filter by register                                   |
| `--source`       | -        | Filter by source (`claude_code`, `chatgpt`, etc.)    |

### `recall ingest`

Re-run the ingestor against `sources.toml`. Incremental by default.

```bash
recall ingest
recall ingest --source claude_code --full     # full re-ingest of one source
recall ingest --since 24h
```

### `recall extract`

BYOK LLM extraction (entities + relationships) over un-extracted drawers.
Crash-safe: pending writes go to `extract_pending`; cost ledger advances
only after database commit.

```bash
recall extract --provider anthropic --model claude-3-5-sonnet
recall extract --resume                       # pick up after a crash
recall extract --rollback <run-id>            # discard a versioned run
```

### `recall status`

Database health: schema version, drawer counts by source, WAL size in
pages, lockfile state.

```bash
recall status
recall status --json                          # machine-readable
```

### `recall verify`

Checksums + FK integrity + FTS rebuild check.

```bash
recall verify
recall verify --rebuild-fts                   # repair if FTS out of sync
```

### `recall backup` / `recall restore`

```bash
recall backup ~/recall-backups/                       # snapshot incl. taxonomy/access/risk
recall restore ~/recall-backups/2026-05-07.db.zst
```

`recall backup` includes `taxonomy_audit`, `access_log`, `risk_audit`,
and `extraction_runs`. With `[backup]` extra installed, the snapshot is
zstandard-compressed.

### `recall forget`

Soft-delete (hide from search; the drawer row stays for audit).

```bash
recall forget abc123de                        # accept unique drawer_uid prefix
recall forget abc123de --dry-run              # preview hide before commit
recall forget --query "old tax info" --apply  # batch hide; --dry-run is implicit otherwise
```

`forget` accepts unique drawer_uid prefixes (git-short-SHA-style); errors
with disambiguation list if the prefix matches multiple drawers.

### `recall lock` (debug)

Diagnose lockfile state. Used when a write is blocked and you want to
see the holder.

```bash
recall lock --status
recall lock --release-stale                   # require holder PID to be dead
```

## Exit codes

| Code | Meaning                                        |
| ---- | ---------------------------------------------- |
| 0    | Success                                        |
| 1    | Generic failure                                |
| 2    | Validation error (bad arguments)               |
| 3    | Lock contention (writer in progress)           |
| 4    | Schema migration required                      |
| 5    | Integrity check failed (`verify` non-zero)     |
