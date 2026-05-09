-- aurochs-recall — T1 extraction layer (v2)
-- Adds the staging table + extraction-runs ledger for the BYOK LLM
-- extraction layer. Both tables are append-only from the application
-- side (status flips happen via UPDATE on the same row). FKs target
-- drawer_meta.drawer_uid so dropping a drawer cascades to its
-- pending/run rows.
--
-- Crash-safety contract (plan v5):
--
--   1. Pre-flight: write a row to ``extract_pending`` BEFORE any LLM
--      call. If the process is killed mid-run, the row stays — the
--      next ``recall extract`` re-picks it up.
--   2. After successful extraction: insert into ``extraction_runs``
--      with status='success' and DELETE the matching pending row in
--      the same transaction.
--   3. On failure (network, budget exhausted, malformed response):
--      insert into ``extraction_runs`` with the appropriate status,
--      and either DELETE the pending row (terminal failure) or leave
--      it (retryable). The runner's policy decides which.
--
-- Cost cap contract:
--
--   * Pre-flight token estimation runs against the prompt + drawer
--     content size (rough char/4 heuristic) to predict input cost.
--   * The runner aborts before issuing the LLM call if the predicted
--     cost would push the running tally over ``budget_usd``.
--   * Mid-run abort: if the API returns a stop-reason indicating budget
--     exhaustion (or the runner detects it via cumulative cost),
--     ``status='budget_exhausted'`` is recorded.
--
-- Versioning contract:
--
--   * ``prompt_version`` is a semver-ish string (e.g. "1.0.0") so we
--     can re-run extractions when prompts change. Old runs stay in the
--     ledger; new runs append.
--   * ``model`` is the literal API model string (e.g.
--     "claude-haiku-4.5", "gpt-4-mini").

CREATE TABLE IF NOT EXISTS extract_pending (
    drawer_uid       TEXT PRIMARY KEY,
    enqueued_at      INTEGER NOT NULL,
    prompt_template  TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    CHECK (LENGTH(prompt_template) > 0),
    CHECK (LENGTH(prompt_version) > 0),
    FOREIGN KEY (drawer_uid) REFERENCES drawer_meta(drawer_uid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_extract_pending_enqueued
  ON extract_pending(enqueued_at);

CREATE TABLE IF NOT EXISTS extraction_runs (
    id               INTEGER PRIMARY KEY,
    drawer_uid       TEXT NOT NULL,
    started_at       INTEGER NOT NULL,
    ended_at         INTEGER,
    status           TEXT NOT NULL,
    model            TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    tokens_input     INTEGER NOT NULL DEFAULT 0,
    tokens_output    INTEGER NOT NULL DEFAULT 0,
    cost_usd         REAL NOT NULL DEFAULT 0.0,
    entities_json    TEXT NOT NULL DEFAULT '[]',
    error_message    TEXT,
    CHECK (status IN ('success', 'partial', 'failed', 'budget_exhausted')),
    CHECK (tokens_input  >= 0),
    CHECK (tokens_output >= 0),
    CHECK (cost_usd      >= 0.0),
    FOREIGN KEY (drawer_uid) REFERENCES drawer_meta(drawer_uid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_extraction_runs_drawer
  ON extraction_runs(drawer_uid);
CREATE INDEX IF NOT EXISTS idx_extraction_runs_status
  ON extraction_runs(status);
CREATE INDEX IF NOT EXISTS idx_extraction_runs_started
  ON extraction_runs(started_at);
