"""SQLite schema and connection handling.

Each table keeps the queryable columns plus the validated Pydantic model as JSON, so
the schema stays small while the domain models remain the single definition of shape.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 8

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

CREATE TABLE IF NOT EXISTS runtime_instances (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    agent_kind TEXT NOT NULL,
    generation INTEGER NOT NULL,
    process_state TEXT NOT NULL,
    owner TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL,
    UNIQUE(agent_id, generation)
);

CREATE TABLE IF NOT EXISTS native_turns (
    id TEXT PRIMARY KEY,
    runtime_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    status TEXT NOT NULL,
    correlation_token TEXT,
    claude_prompt_id TEXT,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_hook_events (
    id TEXT PRIMARY KEY,
    runtime_id TEXT NOT NULL,
    event_name TEXT NOT NULL,
    prompt_id TEXT,
    turn_id TEXT,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_hook_deliveries (
    hook_event_id TEXT PRIMARY KEY,
    delivered_at TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    workflow TEXT NOT NULL,
    status TEXT NOT NULL,
    current_worker_id TEXT,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workers_job ON workers(job_id);
CREATE INDEX IF NOT EXISTS idx_runtime_agent ON runtime_instances(agent_id, generation);
CREATE INDEX IF NOT EXISTS idx_native_turn_runtime ON native_turns(runtime_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_hook_event_runtime ON runtime_hook_events(runtime_id, created_at);
CREATE INDEX IF NOT EXISTS idx_transcript_worker ON transcript(worker_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id, type);
CREATE INDEX IF NOT EXISTS idx_runs_job ON workflow_runs(job_id, updated_at);
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
    """Bring a database up to `SCHEMA_VERSION`.

    Every statement is `IF NOT EXISTS`, and versions so far only add tables and columns
    that default cleanly (a v1 job simply has no composite workflow), so replaying it is the
    whole migration. Anything that needs to rewrite existing rows gets an explicit step.
    """
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    if row is not None and row["version"] < 5:
        _reconcile_open_native_turns(conn)
    if row is not None and row["version"] < 7:
        _rename_ready_to_push(conn)
    if row is not None and row["version"] < 8:
        _rename_behavior_contract(conn)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_native_turn_one_open "
        "ON native_turns(runtime_id) "
        "WHERE status IN ('pending','active','waiting_permission','interrupt_requested')"
    )
    if row is None:
        conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
    elif row["version"] != SCHEMA_VERSION:
        conn.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
    conn.commit()


def _rename_ready_to_push(conn: sqlite3.Connection) -> None:
    """Rewrite the v6 vocabulary that belonged to one workflow rather than to jobs.

    `ready_to_push` was `complete-ticket`'s name for done. Jobs following `rebase` or
    `investigate` now reach the same state, so the attention kind is `work_complete` and
    the stage is a free label. Rows are rewritten rather than tolerated, because an
    unrecognised enum value fails Pydantic validation on load.
    """
    for row in conn.execute(
        "SELECT id, data FROM attention_items WHERE kind='ready_to_push'"
    ).fetchall():
        data = json.loads(row["data"])
        data["kind"] = "work_complete"
        conn.execute(
            "UPDATE attention_items SET kind='work_complete', data=? WHERE id=?",
            (json.dumps(data, separators=(",", ":")), row["id"]),
        )
    # `stage` is now free text, so only the two labels that carried meaning are mapped.
    for old, new in (("ready_to_push", "complete"), ("completed", "complete")):
        for row in conn.execute("SELECT id, data FROM jobs WHERE stage=?", (old,)).fetchall():
            data = json.loads(row["data"])
            data["stage"] = new
            data.setdefault("completed_at", data.get("updated_at"))
            conn.execute(
                "UPDATE jobs SET stage=?, data=? WHERE id=?",
                (new, json.dumps(data, separators=(",", ":")), row["id"]),
            )


def _rename_behavior_contract(conn: sqlite3.Connection) -> None:
    """`behavior_contract` became `goal` when criteria stopped being only about code.

    The body is untouched: the fields did not move, and `Goal` accepts the old criterion
    key names. Only the artifact's type changes, in the column and in the stored model.
    """
    for row in conn.execute(
        "SELECT id, data FROM artifacts WHERE type='behavior_contract'"
    ).fetchall():
        data = json.loads(row["data"])
        data["type"] = "goal"
        conn.execute(
            "UPDATE artifacts SET type='goal', data=? WHERE id=?",
            (json.dumps(data, separators=(",", ":")), row["id"]),
        )


def _reconcile_open_native_turns(conn: sqlite3.Connection) -> None:
    """Preserve but fail older conflicting turns before enforcing the v5 input lane."""
    open_statuses = ("pending", "active", "waiting_permission", "interrupt_requested")
    placeholders = ",".join("?" for _ in open_statuses)
    duplicates = conn.execute(
        f"SELECT runtime_id FROM native_turns WHERE status IN ({placeholders}) "
        "GROUP BY runtime_id HAVING COUNT(*) > 1",
        open_statuses,
    ).fetchall()
    timestamp = datetime.now(UTC).isoformat()
    for duplicate in duplicates:
        rows = conn.execute(
            f"SELECT id, data FROM native_turns WHERE runtime_id=? "
            f"AND status IN ({placeholders}) ORDER BY updated_at DESC, rowid DESC",
            (duplicate["runtime_id"], *open_statuses),
        ).fetchall()
        for stale in rows[1:]:
            data = json.loads(stale["data"])
            data["status"] = "failed"
            data["error"] = "Superseded while migrating duplicate open native turns to v5."
            data["updated_at"] = timestamp
            conn.execute(
                "UPDATE native_turns SET status='failed', updated_at=?, data=? WHERE id=?",
                (timestamp, json.dumps(data, separators=(",", ":")), stale["id"]),
            )
