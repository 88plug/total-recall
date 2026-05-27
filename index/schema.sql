-- total-recall SQLite schema (v4).
--
-- Design notes:
--   * messages          — one row per parsed JSONL record (assistant/user/...).
--                         `byte_offset` is the offset of the LINE START in the
--                         source file, so re-ingest can resume from
--                         `max(byte_offset) + len(line) + 1` (the walker
--                         emits offsets directly so the index doesn't need to
--                         compute them).
--                         v4: `source` tags which CLI client the row came
--                         from ('claude_code', 'codex', 'opencode', ...). The
--                         column is added via migration on pre-v4 DBs with a
--                         default of 'claude_code' so legacy rows stay correct.
--                         `dedup_superseded_by_source` is set when the
--                         cross-source dedup heuristic in
--                         :mod:`index.multi_source` finds the same content in
--                         a higher-priority source.
--   * messages_fts      — FTS5 contentless-ish index over `messages.text`.
--                         `content='messages'` + the ai/ad/au triggers keeps
--                         FTS in sync with the base table automatically.
--   * extractions       — structured facts emitted by extractors/*. One row
--                         per (kind, source_uuid) so re-running an extractor
--                         on the same record is idempotent via the UNIQUE.
--   * extractions_fts   — FTS5 over `extractions.content` for the
--                         user-facing recall queries.
--   * ingest_state      — per-source-file resume cursor. `inode` is checked
--                         so a rotated/replaced file gets re-ingested from
--                         offset 0 instead of producing garbage.
--   * turns             — v2: one row per assistant turn, parsed from JSONL
--                         `message.usage` block. Powers `total-recall
--                         metrics` (token cost, model split, duration).
--   * compactions       — v2: one row per `system.subtype=compact_boundary`
--                         event. Powers compaction-frequency metrics.
--   * ingest_runs       — v2: one row per `total-recall index` run, lifted
--                         from IngestReport. Powers health metrics.
--   * schema_meta       — single-row table of schema version + flags.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY,
    session_id    TEXT    NOT NULL,
    cwd           TEXT,
    git_branch    TEXT,
    role          TEXT    NOT NULL,
    kind          TEXT,
    ts            INTEGER,
    parent_uuid   TEXT,
    message_uuid  TEXT    UNIQUE,
    byte_offset   INTEGER NOT NULL,
    source_file   TEXT    NOT NULL,
    text          TEXT,
    raw_json      BLOB,
    -- v4 multi-source columns. Fresh installs get them straight from this
    -- CREATE; pre-v4 DBs get them via the ALTER TABLE migration in
    -- ``index/db.py::apply_schema``.
    source        TEXT    NOT NULL DEFAULT 'claude_code',
    dedup_superseded_by_source TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_cwd_ts     ON messages(cwd, ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session    ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_kind_ts    ON messages(kind, ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_source     ON messages(source_file, byte_offset);
CREATE INDEX IF NOT EXISTS idx_messages_source_cwd_ts ON messages(source, cwd, ts DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text,
    content='messages',
    content_rowid='id',
    tokenize='porter unicode61',
    columnsize=0
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text)
        VALUES('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text)
        VALUES('delete', old.id, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS extractions (
    id           INTEGER PRIMARY KEY,
    kind         TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    session_id   TEXT    NOT NULL,
    cwd          TEXT,
    ts           INTEGER,
    source_uuid  TEXT,
    score        REAL    DEFAULT 0.5,
    scope        TEXT    DEFAULT 'project',
    context_json TEXT,
    -- v4 multi-source column. Mirrors messages.source so extractor results
    -- can be filtered to a single CLI client without joining back through
    -- messages.
    source       TEXT    NOT NULL DEFAULT 'claude_code',
    dedup_superseded_by_source TEXT,
    UNIQUE(kind, source_uuid)
);

CREATE INDEX IF NOT EXISTS idx_extractions_cwd_kind     ON extractions(cwd, kind, ts DESC);
CREATE INDEX IF NOT EXISTS idx_extractions_session      ON extractions(session_id);
CREATE INDEX IF NOT EXISTS idx_extractions_scope_score  ON extractions(scope, score DESC);
CREATE INDEX IF NOT EXISTS idx_extractions_kind_ts      ON extractions(kind, ts DESC);
CREATE INDEX IF NOT EXISTS idx_extractions_source_kind  ON extractions(source, kind, ts DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS extractions_fts USING fts5(
    content,
    content='extractions',
    content_rowid='id',
    tokenize='porter unicode61',
    columnsize=0
);

CREATE TRIGGER IF NOT EXISTS extractions_ai AFTER INSERT ON extractions BEGIN
    INSERT INTO extractions_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS extractions_ad AFTER DELETE ON extractions BEGIN
    INSERT INTO extractions_fts(extractions_fts, rowid, content)
        VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS extractions_au AFTER UPDATE ON extractions BEGIN
    INSERT INTO extractions_fts(extractions_fts, rowid, content)
        VALUES('delete', old.id, old.content);
    INSERT INTO extractions_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TABLE IF NOT EXISTS ingest_state (
    source_file       TEXT    PRIMARY KEY,
    inode             INTEGER,
    size              INTEGER,
    mtime             INTEGER,
    last_offset       INTEGER NOT NULL DEFAULT 0,
    last_session_id   TEXT,
    records_seen      INTEGER NOT NULL DEFAULT 0,
    errors            INTEGER NOT NULL DEFAULT 0
);

-- v2 metrics tables ---------------------------------------------------------

-- One row per assistant turn, parsed from JSONL `message.usage` block.
CREATE TABLE IF NOT EXISTS turns (
    id                     INTEGER PRIMARY KEY,
    session_id             TEXT    NOT NULL,
    cwd                    TEXT,
    ts                     INTEGER,                  -- unix seconds (assistant timestamp)
    model                  TEXT,                     -- 'claude-opus-4-7', 'claude-sonnet-4-6', etc.
    input_tokens           INTEGER,
    cache_creation_tokens  INTEGER,
    cache_read_tokens      INTEGER,
    output_tokens          INTEGER,
    duration_ms            INTEGER,                  -- from system.subtype=turn_duration if linkable
    stop_reason            TEXT,
    request_id             TEXT,
    message_uuid           TEXT    UNIQUE,           -- dedup key
    source_file            TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_session_ts ON turns(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_turns_cwd_ts     ON turns(cwd, ts DESC);
CREATE INDEX IF NOT EXISTS idx_turns_model_ts   ON turns(model, ts DESC);

-- One row per compaction event from system.subtype='compact_boundary'.
CREATE TABLE IF NOT EXISTS compactions (
    id            INTEGER PRIMARY KEY,
    session_id    TEXT    NOT NULL,
    cwd           TEXT,
    ts            INTEGER,
    pre_tokens    INTEGER,
    post_tokens   INTEGER,
    duration_ms   INTEGER,
    trigger       TEXT,                              -- 'auto' | 'manual'
    message_uuid  TEXT    UNIQUE,
    source_file   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_compactions_cwd_ts  ON compactions(cwd, ts DESC);
CREATE INDEX IF NOT EXISTS idx_compactions_session ON compactions(session_id);

-- One row per total-recall index run (lifted from IngestReport).
CREATE TABLE IF NOT EXISTS ingest_runs (
    id                INTEGER PRIMARY KEY,
    ts                INTEGER NOT NULL,              -- run start
    files_seen        INTEGER NOT NULL,
    files_new         INTEGER NOT NULL,              -- files where new_messages > 0
    new_messages      INTEGER NOT NULL,
    new_extractions   INTEGER NOT NULL,
    new_turns         INTEGER NOT NULL DEFAULT 0,
    new_compactions   INTEGER NOT NULL DEFAULT 0,
    elapsed_ms        INTEGER NOT NULL,
    errors            INTEGER NOT NULL DEFAULT 0,
    trigger           TEXT                           -- 'manual' | 'stop_hook' | 'post_compact_hook' | 'cron'
);
CREATE INDEX IF NOT EXISTS idx_ingest_runs_ts ON ingest_runs(ts DESC);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', '4');
