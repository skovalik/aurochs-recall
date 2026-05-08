-- aurochs-recall — baseline schema (v1)
-- Plan v5 / v4 reference implementation, T0 personal-MVP scope.
--
-- T0 SCOPE NOTE: this baseline includes the long-term storage spine
-- (drawer_meta + drawers_fts), the entity/predicate registries, and the
-- relationship table. Audit / extraction / access-log / hidden-drawers /
-- taxonomy-audit tables are deferred to a later patch — they are not on the
-- read path for the T0 MVP. The schema is designed so those tables can be
-- added in follow-up migrations without altering existing FKs.

-- ============================================================================
-- Drawers (long-term storage)
-- ============================================================================
-- drawer_uid is the stable, content-derived identity used as FK target by
-- relationships (and, in later patches, access_log / risk_audit / etc.).
-- rowid is the SQLite-internal primary key, used only for FTS5 content_rowid.

CREATE TABLE IF NOT EXISTS drawer_meta (
  drawer_uid TEXT NOT NULL UNIQUE,
  rowid INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_path TEXT,
  role TEXT NOT NULL,
  register TEXT,
  thread_id TEXT,
  parent_uid TEXT,
  position_in_thread INTEGER,
  branch_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  risk_score INTEGER NOT NULL DEFAULT 0,
  risk_score_version INTEGER NOT NULL DEFAULT 1,
  hash_input_version INTEGER NOT NULL DEFAULT 1,
  CHECK (LENGTH(drawer_uid) > 0),
  CHECK (LENGTH(role) > 0),
  CHECK (risk_score BETWEEN 0 AND 100),
  FOREIGN KEY (parent_uid) REFERENCES drawer_meta(drawer_uid) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_drawer_meta_unique
  ON drawer_meta(content_hash, source, source_id);
CREATE INDEX IF NOT EXISTS idx_drawer_meta_thread
  ON drawer_meta(thread_id);
CREATE INDEX IF NOT EXISTS idx_drawer_meta_created
  ON drawer_meta(created_at);
CREATE INDEX IF NOT EXISTS idx_drawer_meta_source
  ON drawer_meta(source);

-- FTS5 virtual table holds searchable content. We deliberately keep this
-- self-contained (no external `content=`) because drawer_meta has no
-- `content` column — the split between metadata and indexable text is
-- intentional in v5. Rows are joined to drawer_meta via rowid (FTS5
-- assigns a rowid that we keep in sync with drawer_meta.rowid via the
-- INSERT order in the indexer).
CREATE VIRTUAL TABLE IF NOT EXISTS drawers_fts USING fts5(
  content,
  tokenize='unicode61 remove_diacritics 2'
);

-- ============================================================================
-- Entities + taxonomy registry
-- ============================================================================

CREATE TABLE IF NOT EXISTS entity_types (
  name TEXT PRIMARY KEY,
  description TEXT,
  parent_type TEXT,
  added_at INTEGER NOT NULL,
  added_by TEXT NOT NULL DEFAULT 'seed',
  status TEXT NOT NULL DEFAULT 'active',
  CHECK (LENGTH(name) > 0),
  CHECK (status IN ('active', 'pending-review', 'deprecated')),
  FOREIGN KEY (parent_type) REFERENCES entity_types(name) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS type_aliases (
  alias TEXT NOT NULL,
  canonical TEXT NOT NULL,
  PRIMARY KEY (alias, canonical),
  FOREIGN KEY (canonical) REFERENCES entity_types(name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  metadata TEXT,                          -- JSON-encoded; stdlib json round-trip
  first_seen INTEGER,
  last_seen INTEGER,
  source TEXT NOT NULL DEFAULT 'seed',
  CHECK (LENGTH(name) > 0),
  CHECK (name NOT IN ('null', 'NULL', 'undefined', 'None')),
  CHECK (source IN ('seed', 'llm', 'manual')),
  FOREIGN KEY (type) REFERENCES entity_types(name)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_canonical
  ON entities(LOWER(name), type);
CREATE INDEX IF NOT EXISTS idx_entities_type
  ON entities(type);

-- ============================================================================
-- Predicates + relationships
-- ============================================================================

CREATE TABLE IF NOT EXISTS predicates (
  name TEXT PRIMARY KEY,
  description TEXT,
  domain_type TEXT,
  range_type TEXT,
  symmetric INTEGER NOT NULL DEFAULT 0,
  added_at INTEGER NOT NULL,
  added_by TEXT NOT NULL DEFAULT 'seed',
  status TEXT NOT NULL DEFAULT 'active',
  CHECK (LENGTH(name) > 0),
  CHECK (symmetric IN (0, 1)),
  CHECK (status IN ('active', 'pending-review', 'deprecated'))
);

CREATE TABLE IF NOT EXISTS relationships (
  id INTEGER PRIMARY KEY,
  subject_id INTEGER NOT NULL,
  predicate TEXT NOT NULL,
  object_id INTEGER NOT NULL,
  valid_from INTEGER,
  valid_to INTEGER,
  drawer_uid TEXT,
  metadata TEXT,                          -- JSON
  CHECK (subject_id != object_id),
  FOREIGN KEY (subject_id) REFERENCES entities(id) ON DELETE CASCADE,
  FOREIGN KEY (object_id)  REFERENCES entities(id) ON DELETE CASCADE,
  FOREIGN KEY (drawer_uid) REFERENCES drawer_meta(drawer_uid) ON DELETE SET NULL,
  FOREIGN KEY (predicate)  REFERENCES predicates(name)
);

CREATE INDEX IF NOT EXISTS idx_rel_subject   ON relationships(subject_id);
CREATE INDEX IF NOT EXISTS idx_rel_predicate ON relationships(predicate);
CREATE INDEX IF NOT EXISTS idx_rel_temporal  ON relationships(valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_rel_drawer    ON relationships(drawer_uid);

-- ============================================================================
-- Drawer ↔ entity mentions (join table)
-- ============================================================================
-- The `relationships` table models entity↔entity edges with subject_id and
-- object_id both pointing into `entities`. Its CHECK(subject_id != object_id)
-- intentionally rules out self-loops, which means it cannot directly model
-- "drawer X mentions entity Y" — drawers aren't entities. This dedicated
-- join table holds those mentions cleanly, with the entity row as the FK
-- target on one side and the drawer_uid on the other.
--
-- Always-on linkers (seed-entity matcher) write rows here. Future LLM
-- extraction passes also write here; their output is `detected_by='extractor'`
-- with a confidence score below 1.0 if the model emits one.

CREATE TABLE IF NOT EXISTS drawer_entity_mentions (
  drawer_uid TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  confidence REAL NOT NULL DEFAULT 1.0,
  detected_by TEXT NOT NULL DEFAULT 'linker',  -- 'linker' | 'extractor' | 'manual'
  detected_at INTEGER NOT NULL,
  PRIMARY KEY (drawer_uid, entity_id),
  CHECK (confidence >= 0.0 AND confidence <= 1.0),
  CHECK (detected_by IN ('linker', 'extractor', 'manual')),
  FOREIGN KEY (drawer_uid) REFERENCES drawer_meta(drawer_uid) ON DELETE CASCADE,
  FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_dem_entity   ON drawer_entity_mentions(entity_id);
CREATE INDEX IF NOT EXISTS idx_dem_detected ON drawer_entity_mentions(detected_by);

-- ============================================================================
-- Operational tables
-- ============================================================================

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL,
  description TEXT,
  status TEXT NOT NULL,
  CHECK (status IN ('in_progress', 'applied', 'failed'))
);

CREATE TABLE IF NOT EXISTS index_state (
  source TEXT NOT NULL,
  source_path TEXT NOT NULL,
  last_indexed_mtime INTEGER NOT NULL,
  last_indexed_size INTEGER,
  drawer_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (source, source_path)
);

CREATE TABLE IF NOT EXISTS ingest_errors (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  source_path TEXT,
  reason TEXT NOT NULL,
  fix_hint TEXT,
  occurred_at INTEGER NOT NULL,
  retry_count INTEGER NOT NULL DEFAULT 0
);

-- ============================================================================
-- Seed data
-- ============================================================================
-- Seed entity types and predicates so an empty database is still usable.
-- All seeded rows use added_by = 'seed'; later patches add 'llm' / 'manual'.

INSERT OR IGNORE INTO entity_types (name, description, added_at, added_by, status) VALUES
  ('person',       'A human individual',                                          0, 'seed', 'active'),
  ('project',      'A named project, product, or initiative',                     0, 'seed', 'active'),
  ('concept',      'An abstract idea, methodology, or named concept',             0, 'seed', 'active'),
  ('event',        'A discrete occurrence in time',                               0, 'seed', 'active'),
  ('tool',         'A software tool, library, framework, or utility',             0, 'seed', 'active'),
  ('methodology',  'A system of methods or principles (e.g. PFD)',                0, 'seed', 'active'),
  ('place',        'A physical or virtual location',                              0, 'seed', 'active');

INSERT OR IGNORE INTO predicates (name, description, symmetric, added_at, added_by, status) VALUES
  ('MENTIONS',         'Subject drawer/text mentions object entity',          0, 0, 'seed', 'active'),
  ('AUTHORED_BY',      'Subject artifact authored by object person',          0, 0, 'seed', 'active'),
  ('PART_OF',          'Subject is part of object (composition)',             0, 0, 'seed', 'active'),
  ('USES',             'Subject uses object (tool/methodology)',              0, 0, 'seed', 'active'),
  ('RELATED_TO',       'Symmetric loose-association',                         1, 0, 'seed', 'active'),
  ('LOCATED_IN',       'Subject located in object place',                     0, 0, 'seed', 'active'),
  ('OCCURRED_AT',      'Subject event occurred at object place/time',         0, 0, 'seed', 'active'),
  ('COLLABORATES_WITH','Symmetric collaboration between persons/projects',    1, 0, 'seed', 'active'),
  ('DEPENDS_ON',       'Subject depends on object',                           0, 0, 'seed', 'active'),
  ('SUCCEEDED_BY',     'Subject was succeeded/replaced by object',            0, 0, 'seed', 'active');
