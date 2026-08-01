"""Durable state access. The store -- not any transcript -- is the system of record."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TypeVar
from uuid import UUID

from pydantic import BaseModel

from switchboard.domain.enums import TERMINAL_RUN_STATUSES, ArtifactType, JobStage, WorkerStatus
from switchboard.domain.models import (
    Artifact,
    AttentionItem,
    Decision,
    Event,
    Job,
    Repository,
    RuntimeInstance,
    TranscriptMessage,
    Worker,
    WorkflowExecution,
    WorkflowRun,
    Worktree,
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

    def close(self) -> None:
        self.conn.close()

    # ----------------------------------------------------------- repositories

    def add_repository(self, repo: Repository) -> Repository:
        self.conn.execute(
            "INSERT OR REPLACE INTO repositories (id, name, root_path, data) VALUES (?,?,?,?)",
            (str(repo.id), repo.name, str(repo.root_path), _dump(repo)),
        )
        self.conn.commit()
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

    def get_repository_by_name(self, name: str) -> Repository | None:
        row = self.conn.execute(
            "SELECT data FROM repositories WHERE name=? COLLATE NOCASE", (name,)
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
                job.stage.value,
                job.updated_at.isoformat(),
                _dump(job),
            ),
        )
        self.conn.commit()
        return job

    def get_job(self, job_id: UUID) -> Job | None:
        row = self.conn.execute("SELECT data FROM jobs WHERE id=?", (str(job_id),)).fetchone()
        return _load(Job, row) if row else None

    def list_jobs(self, stage: JobStage | None = None) -> list[Job]:
        if stage:
            rows = self.conn.execute(
                "SELECT data FROM jobs WHERE stage=? ORDER BY updated_at DESC", (stage.value,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT data FROM jobs ORDER BY updated_at DESC").fetchall()
        return [_load(Job, r) for r in rows]

    def active_jobs(self) -> list[Job]:
        closed = (JobStage.COMPLETED.value, JobStage.FAILED.value)
        rows = self.conn.execute(
            "SELECT data FROM jobs WHERE stage NOT IN (?,?) ORDER BY updated_at DESC", closed
        ).fetchall()
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
        self.conn.commit()
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
        self.conn.commit()

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
        self.conn.commit()
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

    def delete_worker(self, worker_id: UUID) -> None:
        self.conn.execute("DELETE FROM workers WHERE id=?", (str(worker_id),))
        self.conn.execute("DELETE FROM transcript WHERE worker_id=?", (str(worker_id),))
        self.conn.execute("DELETE FROM attention_items WHERE worker_id=?", (str(worker_id),))
        self.conn.commit()

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
        self.conn.commit()
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
        self.conn.commit()
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
        self.conn.commit()
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
        self.conn.commit()
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
        self.conn.commit()
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
        self.conn.commit()
        return artifact

    def get_artifact(self, artifact_id: UUID) -> Artifact | None:
        row = self.conn.execute(
            "SELECT data FROM artifacts WHERE id=?", (str(artifact_id),)
        ).fetchone()
        return _load(Artifact, row) if row else None

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
        self.conn.commit()
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
        self.conn.commit()
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
        self.conn.commit()

    def get_preference(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
