# Privacy

aurochs-recall is **explicitly** designed not to transmit your data
anywhere.

## What it does

- Reads the corpora you point it at via `sources.toml`.
- Writes them to a local SQLite database under your user data directory.
- Indexes that database with FTS5.
- Optionally extracts entities + relationships using a BYOK LLM provider
  you explicitly configure.

## What it does not do

- **No telemetry.** Zero phone-home, zero opt-in analytics, zero
  crash reporting.
- **No background uploads.** Nothing in `core/` or `cli/` initiates
  outbound HTTP except to BYOK LLM providers when you explicitly run
  `recall extract` with a configured provider.
- **No vendor lock-in.** The recall.db file is just SQLite. You can
  read it with the standard `sqlite3` CLI, dump it to JSON, or migrate
  to whatever else you want. Nothing about the format depends on this
  package being installed.
- **No cloud component.** There is no aurochs-recall server. There is
  no aurochs-recall account. There is no aurochs-recall API at any
  domain we control.

## CI lint enforcement

The CI configuration includes a lint step that asserts no outbound HTTP
in `core/` to non-whitelisted hosts. The whitelist is `[]` for v0.1.
(Extras can hit BYOK providers because they're opt-in extras; `core/`
cannot.)

## Data handling

| Data                                | Where it goes                                                                  |
| ----------------------------------- | ------------------------------------------------------------------------------ |
| Your conversations                  | Stays on disk; SQLite database in your user data directory                     |
| Embeddings (with `[embeddings]`)    | Stays on disk; either in SQLite blobs or alongside the database                |
| Entity extraction (with BYOK LLM)   | Sent to the provider you explicitly named in your extract command              |
| Access log                          | Stays on disk; never transmitted                                               |
| Crash logs                          | Stays on disk; never transmitted                                               |

## Backup posture

`recall backup` writes a snapshot to a path you specify. It does not
write to any cloud location by default, and there is no cloud-backup
extra. If you want offsite backups, point `recall backup` at a directory
your existing backup system already covers.
