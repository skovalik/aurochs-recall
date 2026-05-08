# Failure modes

What goes wrong, and what the system does about it.

## Crash mid-ingest

- **Symptom:** `recall ingest` interrupted (SIGINT, power loss, OOM).
- **Recovery:** Per-file `index_state` tracks what's been processed.
  Re-running `recall ingest` resumes from the last known-good state.
  Crash-safe by design — no partial drawers in the database.

## Crash mid-extract

- **Symptom:** `recall extract` interrupted while LLM extraction was
  in flight.
- **Recovery:** Pending writes go to `extract_pending` first; the cost
  ledger advances only after database commit. Run
  `recall extract --resume` to pick up where it left off.

## Stale lockfile

- **Symptom:** `recall ingest` blocks with "writer already running"
  but no writer is actually running.
- **Recovery:** The lockfile records the holder PID. On next startup,
  if `psutil.pid_exists(pid)` returns false, the runner force-releases
  the lockfile and logs an audit entry. Manual override:
  `recall lock --release-stale`.

## Schema drift between MCP server and database

- **Symptom:** MCP server connection returns stale results after a
  migration.
- **Recovery:** The MCP server polls `schema_version` periodically. If
  the version changes mid-session, the server recycles its connection.

## FTS5 out of sync with `drawer_meta`

- **Symptom:** `recall verify` reports an FTS rowid mismatch.
- **Recovery:** `recall verify --rebuild-fts` rebuilds the FTS5 index
  from `drawer_meta`. `risk_audit`, `hidden_drawers`, and `access_log`
  are preserved across the rebuild via the stable `drawer_uid`.

## Adversarial content in a drawer

- **Symptom:** A drawer contains a jailbreak template, prompt-injection
  attempt, or bidi/zero-width unicode trickery.
- **Mitigation:** The multi-pass scanner runs on ingest:
  - Raw-byte pass for bidi/ZW characters (+25 risk score).
  - Unicode-stripped pass for jailbreak templates (+40 risk score).
  - Code-block awareness (0.5× multiplier inside fenced blocks).
  - Base64 image-header exemption.
- High-risk drawers can be auto-hidden or flagged for review based on
  the `risk_score` threshold in your config.

## Source file modified during read

- **Symptom:** An ingestor reads a file, then the file changes before
  the read completes.
- **Mitigation:** mtime-after-read check. If the mtime advanced during
  the read, the ingestor discards the partial result and re-queues.

## Disk full during write

- **Symptom:** SQLite write fails with `SQLITE_FULL`.
- **Recovery:** `BEGIN EXCLUSIVE` ensures the transaction rolls back
  cleanly. Free space, retry. The lockfile is released on transaction
  abort.

## Running on an unsupported sqlite version

- **Symptom:** FTS5 tokenizer behavior differs (very rare, but happens
  with vendor-bundled sqlite versions on macOS/Windows).
- **Mitigation:** CI matrix tests both system Python sqlite and the
  `pysqlite3-binary` fallback. If your platform ships an old sqlite,
  the install can switch to the bundled binary on a per-process basis.
- **Status row:** `recall status` prints the active sqlite version so
  you can verify.

## TODO

Add a "what to do when X" runbook for each of the above as the
implementation lands.
