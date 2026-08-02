"""Durable state access. The store -- not any transcript -- is the system of record."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar
from uuid import UUID

from pydantic import BaseModel

from switchboard.domain.enums import (
    TERMINAL_RUN_STATUSES,
    ArtifactType,
    NativeTurnStatus,
    WorkerStatus,
)
from switchboard.domain.models import (
    Artifact,
    AttentionItem,
    Decision,
    Event,
    Job,
    NativeTurn,
    Repository,
    RuntimeHookEvent,
    RuntimeInstance,
    TranscriptMessage,
    Worker,
    WorkflowExecution,
    WorkflowRun,
    Worktree,
    now,
)
from switchboard.storage.database import connect

M = TypeVar("M", bound=BaseModel)


def _dump(model: BaseModel) -> str:
    return model.model_dump_json()


def _load(model_cls: type[M], row: sqlite3.Row) -> M:
    return model_cls.model_validate_json(row["data"])


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = connect(path)
        self._transaction_depth = 0

    def close(self) -> None:
        self.conn.close()

    def _commit(self) -> None:
        if self._transaction_depth == 0:
            self.conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Make all Store writes in the block one SQLite commit boundary."""
        outermost = self._transaction_depth == 0
        if outermost:
            self.conn.execute("BEGIN IMMEDIATE")
        self._transaction_depth += 1
        try:
            yield
        except BaseException:
            self._transaction_depth -= 1
            if outermost:
                self.conn.rollback()
            raise
        else:
            self._transaction_depth -= 1
            if outermost:
                self.conn.commit()

    # ----------------------------------------------------------- repositories

    def add_repository(self, repo: Repository) -> Repository:
        self.conn.execute(
            "INSERT OR REPLACE INTO repositories (id, name, root_path, data) VALUES (?,?,?,?)",
            (str(repo.id), repo.name, str(repo.root_path), _dump(repo)),
        )
        self._commit()
        return repo

    def get_repository(self, repo_id: UUID) -> Repository | None:
        row = self.conn.execute(
            "SELECT data FROM repositories WHERE id=?", (str(repo_id),)
        ).fetchone()
        return _load(Repository, row) if row else None

    def get_repository_by_path(self, path: Path) -> Repository | None:
        row = self.conn.execute(
            "SELECT data FROM repositories WHERE root_path=?", (str(path),)
        ).fetchone()
        return _load(Repository, row) if row else None

    def list_repositories(self) -> list[Repository]:
        rows = self.conn.execute("SELECT data FROM repositories ORDER BY name").fetchall()
        return [_load(Repository, r) for r in rows]

    # -------------------------------------------------------------------- jobs

    def save_job(self, job: Job) -> Job:
        self.conn.execute(
            "INSERT OR REPLACE INTO jobs (id, repository_id, external_ref, stage, updated_at, data)"
            " VALUES (?,?,?,?,?,?)",
            (
                str(job.id),
                str(job.repository_id),
                job.external_ref,
                job.stage,
                job.updated_at.isoformat(),
                _dump(job),
            ),
        )
        self._commit()
        return job

    def get_job(self, job_id: UUID) -> Job | None:
        row = self.conn.execute("SELECT data FROM jobs WHERE id=?", (str(job_id),)).fetchone()
        return _load(Job, row) if row else None

    def list_jobs(self, stage: str | None = None) -> list[Job]:
        if stage:
            rows = self.conn.execute(
                "SELECT data FROM jobs WHERE stage=? ORDER BY updated_at DESC", (stage,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT data FROM jobs ORDER BY updated_at DESC").fetchall()
        return [_load(Job, r) for r in rows]

    # --------------------------------------------------------------- worktrees

    def save_worktree(self, worktree: Worktree) -> Worktree:
        self.conn.execute(
            "INSERT OR REPLACE INTO worktrees (id, repository_id, path, owner_worker_id, data)"
            " VALUES (?,?,?,?,?)",
            (
                str(worktree.id),
                str(worktree.repository_id),
                str(worktree.path),
                str(worktree.owner_worker_id) if worktree.owner_worker_id else None,
                _dump(worktree),
            ),
        )
        self._commit()
        return worktree

    def get_worktree(self, worktree_id: UUID) -> Worktree | None:
        row = self.conn.execute(
            "SELECT data FROM worktrees WHERE id=?", (str(worktree_id),)
        ).fetchone()
        return _load(Worktree, row) if row else None

    def list_worktrees(self, repository_id: UUID | None = None) -> list[Worktree]:
        if repository_id:
            rows = self.conn.execute(
                "SELECT data FROM worktrees WHERE repository_id=?", (str(repository_id),)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT data FROM worktrees").fetchall()
        return [_load(Worktree, r) for r in rows]

    def delete_worktree(self, worktree_id: UUID) -> None:
        self.conn.execute("DELETE FROM worktrees WHERE id=?", (str(worktree_id),))
        self._commit()

    # ----------------------------------------------------------------- workers

    def save_worker(self, worker: Worker) -> Worker:
        self.conn.execute(
            "INSERT OR REPLACE INTO workers"
            " (id, job_id, repository_id, worktree_id, role, status, writable, created_at, data)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(worker.id),
                str(worker.job_id) if worker.job_id else None,
                str(worker.repository_id),
                str(worker.worktree_id) if worker.worktree_id else None,
                worker.role.value,
                worker.status.value,
                int(worker.writable),
                worker.created_at.isoformat(),
                _dump(worker),
            ),
        )
        self._commit()
        return worker

    def get_worker(self, worker_id: UUID) -> Worker | None:
        row = self.conn.execute("SELECT data FROM workers WHERE id=?", (str(worker_id),)).fetchone()
        return _load(Worker, row) if row else None

    def list_workers(
        self, job_id: UUID | None = None, status: WorkerStatus | None = None
    ) -> list[Worker]:
        clauses, params = [], []
        if job_id:
            clauses.append("job_id=?")
            params.append(str(job_id))
        if status:
            clauses.append("status=?")
            params.append(status.value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT data FROM workers{where} ORDER BY created_at, rowid", params
        ).fetchall()
        return [_load(Worker, r) for r in rows]

    # --------------------------------------------------------------- runtimes

    def save_runtime(self, runtime: RuntimeInstance) -> RuntimeInstance:
        self.conn.execute(
            "INSERT OR REPLACE INTO runtime_instances"
            " (id, agent_id, agent_kind, generation, process_state, owner, updated_at, data)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                str(runtime.id),
                str(runtime.agent_id),
                runtime.agent_kind.value,
                runtime.generation,
                runtime.process_state.value,
                runtime.owner.value,
                runtime.updated_at.isoformat(),
                _dump(runtime),
            ),
        )
        self._commit()
        return runtime

    def get_runtime(self, runtime_id: UUID) -> RuntimeInstance | None:
        row = self.conn.execute(
            "SELECT data FROM runtime_instances WHERE id=?", (str(runtime_id),)
        ).fetchone()
        return _load(RuntimeInstance, row) if row else None

    def current_runtime(self, agent_id: UUID) -> RuntimeInstance | None:
        row = self.conn.execute(
            "SELECT data FROM runtime_instances WHERE agent_id=?"
            " ORDER BY generation DESC LIMIT 1",
            (str(agent_id),),
        ).fetchone()
        return _load(RuntimeInstance, row) if row else None

    def list_runtimes(self, agent_id: UUID | None = None) -> list[RuntimeInstance]:
        if agent_id is None:
            rows = self.conn.execute(
                "SELECT data FROM runtime_instances ORDER BY agent_id, generation"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT data FROM runtime_instances WHERE agent_id=? ORDER BY generation",
                (str(agent_id),),
            ).fetchall()
        return [_load(RuntimeInstance, row) for row in rows]

    # ------------------------------------------------------------ native turns

    def save_native_turn(self, turn: NativeTurn) -> NativeTurn:
        self.conn.execute(
            "INSERT INTO native_turns"
            " (id, runtime_id, origin, status, correlation_token, claude_prompt_id,"
            " updated_at, data) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " runtime_id=excluded.runtime_id, origin=excluded.origin, status=excluded.status,"
            " correlation_token=excluded.correlation_token,"
            " claude_prompt_id=excluded.claude_prompt_id,"
            " updated_at=excluded.updated_at, data=excluded.data",
            (
                str(turn.id),
                str(turn.runtime_id),
                turn.origin.value,
                turn.status.value,
                turn.correlation_token,
                turn.claude_prompt_id,
                turn.updated_at.isoformat(),
                _dump(turn),
            ),
        )
        self._commit()
        return turn

    def get_native_turn(self, turn_id: UUID) -> NativeTurn | None:
        row = self.conn.execute(
            "SELECT data FROM native_turns WHERE id=?", (str(turn_id),)
        ).fetchone()
        return _load(NativeTurn, row) if row else None

    def native_turn_by_token(self, runtime_id: UUID, token: str) -> NativeTurn | None:
        row = self.conn.execute(
            "SELECT data FROM native_turns WHERE runtime_id=? AND correlation_token=?"
            " ORDER BY updated_at DESC LIMIT 1",
            (str(runtime_id), token),
        ).fetchone()
        return _load(NativeTurn, row) if row else None

    def active_native_turn(
        self, runtime_id: UUID, prompt_id: str | None = None
    ) -> NativeTurn | None:
        statuses = (
            NativeTurnStatus.ACTIVE.value,
            NativeTurnStatus.WAITING_PERMISSION.value,
            NativeTurnStatus.INTERRUPT_REQUESTED.value,
        )
        if prompt_id:
            row = self.conn.execute(
                "SELECT data FROM native_turns WHERE runtime_id=? AND claude_prompt_id=?"
                " AND status IN (?,?,?) ORDER BY updated_at DESC LIMIT 1",
                (str(runtime_id), prompt_id, *statuses),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT data FROM native_turns WHERE runtime_id=? AND status IN (?,?,?)"
                " ORDER BY updated_at DESC LIMIT 1",
                (str(runtime_id), *statuses),
            ).fetchone()
        return _load(NativeTurn, row) if row else None

    def open_native_turn(self, runtime_id: UUID) -> NativeTurn | None:
        """Return a turn that still owns the runtime's single input lane."""
        statuses = (
            NativeTurnStatus.PENDING.value,
            NativeTurnStatus.ACTIVE.value,
            NativeTurnStatus.WAITING_PERMISSION.value,
            NativeTurnStatus.INTERRUPT_REQUESTED.value,
        )
        row = self.conn.execute(
            "SELECT data FROM native_turns WHERE runtime_id=? AND status IN (?,?,?,?)"
            " ORDER BY updated_at DESC, rowid DESC LIMIT 1",
            (str(runtime_id), *statuses),
        ).fetchone()
        return _load(NativeTurn, row) if row else None

    def list_native_turns(self, runtime_id: UUID) -> list[NativeTurn]:
        rows = self.conn.execute(
            "SELECT data FROM native_turns WHERE runtime_id=? ORDER BY updated_at, rowid",
            (str(runtime_id),),
        ).fetchall()
        return [_load(NativeTurn, row) for row in rows]

    def add_runtime_hook_event(self, event: RuntimeHookEvent) -> RuntimeHookEvent:
        self.conn.execute(
            "INSERT INTO runtime_hook_events"
            " (id, runtime_id, event_name, prompt_id, turn_id, created_at, data)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                str(event.id),
                str(event.runtime_id),
                event.event_name,
                event.prompt_id,
                str(event.turn_id) if event.turn_id else None,
                event.created_at.isoformat(),
                _dump(event),
            ),
        )
        self._commit()
        return event

    def runtime_hook_events(self, runtime_id: UUID) -> list[RuntimeHookEvent]:
        rows = self.conn.execute(
            "SELECT data FROM runtime_hook_events WHERE runtime_id=? ORDER BY created_at, rowid",
            (str(runtime_id),),
        ).fetchall()
        return [_load(RuntimeHookEvent, row) for row in rows]

    def pending_worker_hook_events(self, runtime_id: UUID) -> list[RuntimeHookEvent]:
        rows = self.conn.execute(
            "SELECT e.data FROM runtime_hook_events e "
            "LEFT JOIN worker_hook_deliveries d ON d.hook_event_id=e.id "
            "WHERE e.runtime_id=? AND d.hook_event_id IS NULL "
            "ORDER BY e.created_at, e.rowid",
            (str(runtime_id),),
        ).fetchall()
        return [_load(RuntimeHookEvent, row) for row in rows]

    def mark_worker_hook_delivered(self, event_id: UUID) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO worker_hook_deliveries (hook_event_id, delivered_at) "
            "VALUES (?,?)",
            (str(event_id), now().isoformat()),
        )
        self._commit()

    def worker_hook_delivered(self, event_id: UUID) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM worker_hook_deliveries WHERE hook_event_id=?", (str(event_id),)
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ events

    def add_event(self, event: Event) -> Event:
        self.conn.execute(
            "INSERT INTO events (id, kind, job_id, worker_id, created_at, data) VALUES (?,?,?,?,?,?)",
            (
                str(event.id),
                event.kind,
                str(event.job_id) if event.job_id else None,
                str(event.worker_id) if event.worker_id else None,
                event.created_at.isoformat(),
                _dump(event),
            ),
        )
        self._commit()
        return event

    def recent_events(self, limit: int = 10, job_id: UUID | None = None) -> list[Event]:
        if job_id:
            rows = self.conn.execute(
                "SELECT data FROM events WHERE job_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (str(job_id), limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT data FROM events ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_load(Event, r) for r in rows]

    # --------------------------------------------------------------- attention

    def save_attention_item(self, item: AttentionItem) -> AttentionItem:
        self.conn.execute(
            "INSERT OR REPLACE INTO attention_items"
            " (id, worker_id, job_id, kind, handled, created_at, data) VALUES (?,?,?,?,?,?,?)",
            (
                str(item.id),
                str(item.worker_id),
                str(item.job_id) if item.job_id else None,
                item.kind.value,
                int(item.handled),
                item.created_at.isoformat(),
                _dump(item),
            ),
        )
        self._commit()
        return item

    def list_attention_items(self, include_handled: bool = False) -> list[AttentionItem]:
        sql = "SELECT data FROM attention_items"
        if not include_handled:
            sql += " WHERE handled=0"
        sql += " ORDER BY created_at, rowid"
        return [_load(AttentionItem, r) for r in self.conn.execute(sql).fetchall()]

    def attention_items_for_worker(self, worker_id: UUID) -> list[AttentionItem]:
        rows = self.conn.execute(
            "SELECT data FROM attention_items WHERE worker_id=? AND handled=0 ORDER BY created_at, rowid",
            (str(worker_id),),
        ).fetchall()
        return [_load(AttentionItem, r) for r in rows]

    # -------------------------------------------------------------- transcript

    def add_transcript(self, message: TranscriptMessage) -> TranscriptMessage:
        self.conn.execute(
            "INSERT INTO transcript (id, worker_id, created_at, data) VALUES (?,?,?,?)",
            (
                str(message.id),
                str(message.worker_id),
                message.created_at.isoformat(),
                _dump(message),
            ),
        )
        self._commit()
        return message

    def transcript(self, worker_id: UUID, limit: int = 500) -> list[TranscriptMessage]:
        rows = self.conn.execute(
            "SELECT data FROM transcript WHERE worker_id=? ORDER BY created_at, rowid LIMIT ?",
            (str(worker_id), limit),
        ).fetchall()
        return [_load(TranscriptMessage, r) for r in rows]

    # --------------------------------------------------------------- decisions

    def add_decision(self, decision: Decision) -> Decision:
        self.conn.execute(
            "INSERT INTO decisions (id, job_id, created_at, data) VALUES (?,?,?,?)",
            (str(decision.id), str(decision.job_id), decision.created_at.isoformat(), _dump(decision)),
        )
        self._commit()
        return decision

    def list_decisions(self, job_id: UUID) -> list[Decision]:
        rows = self.conn.execute(
            "SELECT data FROM decisions WHERE job_id=? ORDER BY created_at, rowid", (str(job_id),)
        ).fetchall()
        return [_load(Decision, r) for r in rows]

    # --------------------------------------------------------------- artifacts

    def save_artifact(self, artifact: Artifact) -> Artifact:
        self.conn.execute(
            "INSERT OR REPLACE INTO artifacts (id, job_id, type, stale, created_at, data)"
            " VALUES (?,?,?,?,?,?)",
            (
                str(artifact.id),
                str(artifact.job_id),
                artifact.type.value,
                int(artifact.stale),
                artifact.created_at.isoformat(),
                _dump(artifact),
            ),
        )
        self._commit()
        return artifact

    def list_artifacts(self, job_id: UUID, type_: ArtifactType | None = None) -> list[Artifact]:
        if type_:
            rows = self.conn.execute(
                "SELECT data FROM artifacts WHERE job_id=? AND type=? ORDER BY created_at, rowid",
                (str(job_id), type_.value),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT data FROM artifacts WHERE job_id=? ORDER BY created_at, rowid", (str(job_id),)
            ).fetchall()
        return [_load(Artifact, r) for r in rows]

    def latest_artifact(self, job_id: UUID, type_: ArtifactType) -> Artifact | None:
        items = self.list_artifacts(job_id, type_)
        return items[-1] if items else None

    # ----------------------------------------------------- workflow executions

    def add_workflow_execution(self, execution: WorkflowExecution) -> WorkflowExecution:
        self.conn.execute(
            "INSERT OR REPLACE INTO workflow_executions"
            " (id, job_id, worker_id, workflow, created_at, data) VALUES (?,?,?,?,?,?)",
            (
                str(execution.id),
                str(execution.job_id) if execution.job_id else None,
                str(execution.worker_id),
                execution.workflow,
                execution.created_at.isoformat(),
                _dump(execution),
            ),
        )
        self._commit()
        return execution

    def list_workflow_executions(self, job_id: UUID) -> list[WorkflowExecution]:
        rows = self.conn.execute(
            "SELECT data FROM workflow_executions WHERE job_id=? ORDER BY created_at, rowid",
            (str(job_id),),
        ).fetchall()
        return [_load(WorkflowExecution, r) for r in rows]

    def recent_workflow_executions(self, limit: int = 200) -> list[WorkflowExecution]:
        """Every job's executions, oldest first, bounded.

        Ordered chronologically rather than by recency because the only question worth
        asking of this list is what the user does *in sequence*.
        """
        rows = self.conn.execute(
            "SELECT data FROM ("
            "  SELECT data, created_at, rowid FROM workflow_executions"
            "  ORDER BY created_at DESC, rowid DESC LIMIT ?"
            ") ORDER BY created_at, rowid",
            (limit,),
        ).fetchall()
        return [_load(WorkflowExecution, r) for r in rows]

    # ------------------------------------------------------------ workflow runs

    def save_run(self, run: WorkflowRun) -> WorkflowRun:
        self.conn.execute(
            "INSERT OR REPLACE INTO workflow_runs"
            " (id, job_id, workflow, status, current_worker_id, updated_at, data)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                str(run.id),
                str(run.job_id),
                run.workflow,
                run.status.value,
                str(run.current_worker_id) if run.current_worker_id else None,
                run.updated_at.isoformat(),
                _dump(run),
            ),
        )
        self._commit()
        return run

    def get_run(self, run_id: UUID) -> WorkflowRun | None:
        row = self.conn.execute(
            "SELECT data FROM workflow_runs WHERE id=?", (str(run_id),)
        ).fetchone()
        return _load(WorkflowRun, row) if row else None

    def list_runs(self, job_id: UUID | None = None) -> list[WorkflowRun]:
        if job_id:
            rows = self.conn.execute(
                "SELECT data FROM workflow_runs WHERE job_id=? ORDER BY updated_at, rowid",
                (str(job_id),),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT data FROM workflow_runs ORDER BY updated_at, rowid"
            ).fetchall()
        return [_load(WorkflowRun, r) for r in rows]

    def active_run(self, job_id: UUID) -> WorkflowRun | None:
        """The job's live run, if any. There is at most one."""
        placeholders = ",".join("?" * len(TERMINAL_RUN_STATUSES))
        rows = self.conn.execute(
            f"SELECT data FROM workflow_runs WHERE job_id=? AND status NOT IN ({placeholders})"
            " ORDER BY updated_at DESC, rowid DESC LIMIT 1",
            (str(job_id), *sorted(s.value for s in TERMINAL_RUN_STATUSES)),
        ).fetchall()
        return _load(WorkflowRun, rows[0]) if rows else None

    def run_for_worker(self, worker_id: UUID) -> WorkflowRun | None:
        rows = self.conn.execute(
            "SELECT data FROM workflow_runs WHERE current_worker_id=?"
            " ORDER BY updated_at DESC, rowid DESC LIMIT 1",
            (str(worker_id),),
        ).fetchall()
        return _load(WorkflowRun, rows[0]) if rows else None

    # ------------------------------------------------------------- preferences

    def set_preference(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO preferences (key, value) VALUES (?,?)", (key, value)
        )
        self._commit()

    def get_preference(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def get_or_create_preference(self, key: str, value: str) -> str:
        """Atomically establish singleton identity shared by competing controllers."""
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO preferences (key, value) VALUES (?,?)", (key, value)
            )
            row = self.conn.execute(
                "SELECT value FROM preferences WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            raise RuntimeError(f"Could not establish preference {key!r}.")
        return str(row["value"])
