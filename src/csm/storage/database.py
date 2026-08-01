"""SQLite schema and connection handling.

Each table keeps the queryable columns plus the validated Pydantic model as JSON, so
the schema stays small while the domain models remain the single definition of shape.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS repositories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL UNIQUE,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    external_ref TEXT,
    stage TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worktrees (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    owner_worker_id TEXT,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    repository_id TEXT NOT NULL,
    worktree_id TEXT,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    writable INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    job_id TEXT,
    worker_id TEXT,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attention_items (
    id TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    job_id TEXT,
    kind TEXT NOT NULL,
    handled INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcript (
    id TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    type TEXT NOT NULL,
    stale INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_executions (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    worker_id TEXT NOT NULL,
    workflow TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workers_job ON workers(job_id);
CREATE INDEX IF NOT EXISTS idx_transcript_worker ON transcript(worker_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id, type);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Create the schema if absent. Version 1 is the initial schema."""
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()
