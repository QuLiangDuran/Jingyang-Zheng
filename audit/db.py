import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    source_path TEXT,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id          INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    idx         INTEGER NOT NULL,
    start       INTEGER NOT NULL,
    "end"       INTEGER NOT NULL,
    UNIQUE (document_id, idx)
);

CREATE TABLE IF NOT EXISTS extraction_runs (
    id             INTEGER PRIMARY KEY,
    document_id    INTEGER NOT NULL REFERENCES documents(id),
    segment_id     INTEGER NOT NULL REFERENCES segments(id),
    model          TEXT NOT NULL,
    base_url       TEXT,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    temperature    REAL,
    prompt         TEXT NOT NULL,
    raw_response   TEXT,
    status         TEXT NOT NULL CHECK (status IN ('ok', 'parse_error', 'llm_error')),
    error          TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES extraction_runs(id),
    document_id   INTEGER NOT NULL REFERENCES documents(id),
    segment_id    INTEGER NOT NULL REFERENCES segments(id),
    kind          TEXT NOT NULL CHECK (kind IN ('source', 'condition', 'observation')),
    -- observation -> its condition, condition -> its source; resolved by the system from link labels
    parent_id     INTEGER REFERENCES records(id),
    proposed_json TEXT NOT NULL,
    current_json  TEXT NOT NULL,
    flags_json    TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'approved', 'rejected', 'needs_resolution')),
    reviewer      TEXT,
    reviewed_at   TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_log (
    id         INTEGER PRIMARY KEY,
    record_id  INTEGER NOT NULL REFERENCES records(id),
    reviewer   TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('approve', 'edit', 'reject', 'reopen', 'flag')),
    field      TEXT,
    old_value  TEXT,
    new_value  TEXT,
    reason     TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_records_status ON records(status);
CREATE INDEX IF NOT EXISTS idx_records_document ON records(document_id);
CREATE INDEX IF NOT EXISTS idx_records_parent ON records(parent_id);
CREATE INDEX IF NOT EXISTS idx_log_record ON review_log(record_id);

CREATE TRIGGER IF NOT EXISTS records_proposed_immutable
BEFORE UPDATE OF proposed_json ON records
BEGIN SELECT RAISE(ABORT, 'proposed_json is immutable'); END;

CREATE TRIGGER IF NOT EXISTS runs_raw_immutable
BEFORE UPDATE OF raw_response ON extraction_runs
BEGIN SELECT RAISE(ABORT, 'raw_response is immutable'); END;

CREATE TRIGGER IF NOT EXISTS review_log_no_update
BEFORE UPDATE ON review_log
BEGIN SELECT RAISE(ABORT, 'review_log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS review_log_no_delete
BEFORE DELETE ON review_log
BEGIN SELECT RAISE(ABORT, 'review_log is append-only'); END;
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(records)")}
    if cols and "kind" not in cols:
        conn.close()
        raise RuntimeError(f"{path} 是旧版（示例字段）数据库，和三层结构不兼容。"
                           "请先把它改名备份，再重新运行。")
    conn.executescript(SCHEMA)
    return conn
