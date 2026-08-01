"""Storage migrations preserve evidence while establishing new invariants."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from switchboard.domain.enums import NativeTurnOrigin, NativeTurnStatus
from switchboard.domain.models import NativeTurn
from switchboard.storage.database import SCHEMA_VERSION, connect


def test_v5_migration_fails_older_duplicate_open_turn_without_losing_it(tmp_path: Path):
    path = tmp_path / "v4.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE schema_meta (version INTEGER NOT NULL);
        INSERT INTO schema_meta VALUES (4);
        CREATE TABLE native_turns (
            id TEXT PRIMARY KEY, runtime_id TEXT NOT NULL, origin TEXT NOT NULL,
            status TEXT NOT NULL, correlation_token TEXT, claude_prompt_id TEXT,
            updated_at TEXT NOT NULL, data TEXT NOT NULL
        );
        """
    )
    runtime_id = uuid4()
    older = NativeTurn(runtime_id=runtime_id, origin=NativeTurnOrigin.MANAGED)
    newer = NativeTurn(runtime_id=runtime_id, origin=NativeTurnOrigin.HUMAN)
    for index, turn in enumerate((older, newer)):
        raw.execute(
            "INSERT INTO native_turns VALUES (?,?,?,?,?,?,?,?)",
            (
                str(turn.id),
                str(runtime_id),
                turn.origin.value,
                turn.status.value,
                turn.correlation_token,
                turn.claude_prompt_id,
                f"2026-08-01T00:00:0{index}+00:00",
                turn.model_dump_json(),
            ),
        )
    raw.commit()
    raw.close()

    migrated = connect(path)
    rows = migrated.execute(
        "SELECT id, status, data FROM native_turns ORDER BY id"
    ).fetchall()

    assert migrated.execute("SELECT version FROM schema_meta").fetchone()[0] == SCHEMA_VERSION
    assert sorted(row["status"] for row in rows) == ["failed", "pending"]
    failed = next(row for row in rows if row["status"] == "failed")
    assert failed["id"] == str(older.id)
    assert json.loads(failed["data"])["status"] == NativeTurnStatus.FAILED.value
    assert "Superseded" in json.loads(failed["data"])["error"]
    migrated.close()
