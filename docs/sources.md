# Sources schema

`sources.toml` declares which corpora aurochs-recall ingests. The full
schema is documented here; the [contracts.md](contracts.md) page has the
machine-readable version.

## Location

| OS                    | Path                                                                     |
| --------------------- | ------------------------------------------------------------------------ |
| Linux                 | `~/.config/aurochs-recall/sources.toml`                                  |
| macOS                 | `~/Library/Application Support/aurochs-recall/sources.toml`              |
| Windows               | `%APPDATA%\aurochs-recall\sources.toml`                                  |

`recall init` creates the file. You're expected to edit it as you add or
remove sources.

## Shape

```toml
# sources.toml

[meta]
schema_version = 1                 # bumps with breaking schema changes

[[source]]
id = "claude_code_personal"        # stable id; used in drawer_uid
type = "claude_code"               # ingestor type — see "Supported types" below
roots = [
    "~/.claude/projects",
]
enabled = true

[[source]]
id = "claude_ai_export_2026-04"
type = "claude_ai"
roots = [
    "~/Downloads/claude-export-2026-04.zip",
]
enabled = true

[[source]]
id = "chatgpt_export_2026-04"
type = "chatgpt"
roots = [
    "~/Downloads/chatgpt-export-2026-04.zip",
]
enabled = false                     # not currently ingested

[[source]]
id = "agency_wiki"
type = "markdown"
roots = [
    "~/Work/Aurochs/wiki",
]
include = ["**/*.md"]
exclude = ["**/_drafts/**"]
enabled = true
```

## Supported types

| `type`         | Inputs                                                       | Notes                                                                        |
| -------------- | ------------------------------------------------------------ | ---------------------------------------------------------------------------- |
| `claude_code`  | `.jsonl` session files under `~/.claude/projects/<proj>/`    | Each session yields a thread; messages become drawers                        |
| `claude_ai`    | claude.ai export `.zip` or unpacked `conversations.json`     | One thread per conversation                                                  |
| `chatgpt`      | ChatGPT export `.zip` or unpacked `conversations.json`       | Mapping tree traversed depth-first for deterministic order                   |
| `markdown`     | Directory tree of `.md` files                                | One drawer per chunk; chunk strategy configurable in a future schema version |
| `capture`      | `/stefan capture` voice/text observations                    | Streamed in via the capture pipeline                                         |

(See `core/ingest/<type>.py` for the canonical mapping rule for each.)

## Field reference

| Field         | Required | Default | Meaning                                                            |
| ------------- | -------- | ------- | ------------------------------------------------------------------ |
| `id`          | yes      | -       | Stable string used in `drawer_uid` construction                    |
| `type`        | yes      | -       | One of the supported types                                         |
| `roots`       | yes      | -       | One or more file paths or directory roots                          |
| `enabled`     | no       | `true`  | If `false`, the source is skipped during ingest                    |
| `include`     | no       | -       | Glob patterns to include (default: type-specific)                  |
| `exclude`     | no       | -       | Glob patterns to exclude                                           |
| `since`       | no       | -       | Only ingest entries newer than this date (`YYYY-MM-DD`)            |

## Per-ingestor schema mapping

Each ingestor maps source-specific fields onto the canonical drawer
schema. The full mapping table is in [contracts.md](contracts.md).
