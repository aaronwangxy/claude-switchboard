"""Validated domain models persisted in SQLite."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from csm.domain.enums import (
    ArtifactType,
    AttentionKind,
    JobStage,
    RunStatus,
    WorkerRole,
    WorkerStatus,
)


def now() -> datetime:
    return datetime.now(UTC)


class Base(BaseModel):
    model_config = ConfigDict(use_enum_values=False, validate_assignment=True)


class Repository(Base):
    id: UUID = Field(default_factory=uuid4)
    name: str
    root_path: Path
    default_branch: str = "main"
    registered_at: datetime = Field(default_factory=now)


class Job(Base):
    id: UUID = Field(default_factory=uuid4)
    title: str
    external_ref: str | None = None
    repository_id: UUID
    stage: JobStage = JobStage.INTAKE
    selected_worker_id: UUID | None = None
    base_ref: str = "main"
    ticket_text: str = ""
    #: The composite workflow this job follows. Stored so a resumed job is reproducible.
    profile: str | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class Worktree(Base):
    id: UUID = Field(default_factory=uuid4)
    repository_id: UUID
    path: Path
    branch: str
    base_ref: str
    owner_worker_id: UUID | None = None
    created_at: datetime = Field(default_factory=now)


class Worker(Base):
    id: UUID = Field(default_factory=uuid4)
    job_id: UUID | None = None
    title: str
    role: WorkerRole = WorkerRole.GENERAL
    status: WorkerStatus = WorkerStatus.STARTING
    repository_id: UUID
    cwd: Path
    worktree_id: UUID | None = None
    session_id: str | None = None
    model: str | None = None
    waiting_for: str | None = None
    writable: bool = False
    pinned: bool = False
    snoozed_until: datetime | None = None
    workflow: str | None = None
    prompt_policy_version: str = "1"
    active_helpers: int = 0
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class Event(Base):
    id: UUID = Field(default_factory=uuid4)
    kind: str
    job_id: UUID | None = None
    worker_id: UUID | None = None
    summary: str = ""
    payload: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now)


class AttentionItem(Base):
    id: UUID = Field(default_factory=uuid4)
    worker_id: UUID
    job_id: UUID | None = None
    kind: AttentionKind
    reason: str
    waiting_for: str | None = None
    handled: bool = False
    created_at: datetime = Field(default_factory=now)


class TranscriptMessage(Base):
    id: UUID = Field(default_factory=uuid4)
    worker_id: UUID
    role: str  # user | assistant | tool | system
    text: str
    created_at: datetime = Field(default_factory=now)


class Decision(Base):
    id: UUID = Field(default_factory=uuid4)
    job_id: UUID
    question: str
    answer: str
    created_at: datetime = Field(default_factory=now)


class Artifact(Base):
    """A stored structured artifact with Git lineage for freshness checks."""

    id: UUID = Field(default_factory=uuid4)
    job_id: UUID
    type: ArtifactType
    worker_id: UUID | None = None
    base_commit: str | None = None
    head_commit: str | None = None
    tree_hash: str | None = None
    stale: bool = False
    stale_reason: str | None = None
    body: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now)


class WorkflowExecution(Base):
    id: UUID = Field(default_factory=uuid4)
    job_id: UUID | None = None
    worker_id: UUID
    workflow: str
    head_commit: str | None = None
    status: str = "started"  # started | completed | failed
    created_at: datetime = Field(default_factory=now)


class WorkflowRun(Base):
    """One execution of a composite workflow over a job.

    The run -- not the manager's memory -- is what knows which step a job is on, how many
    bounded repeats a step has used, and why it is paused.
    """

    id: UUID = Field(default_factory=uuid4)
    job_id: UUID
    workflow: str
    request: str = ""
    step_index: int = 0
    #: str(step index) -> how many times that step has run in this run.
    iterations: dict[str, int] = Field(default_factory=dict)
    #: Step indices the user has explicitly approved.
    approved_steps: list[int] = Field(default_factory=list)
    status: RunStatus = RunStatus.RUNNING
    current_worker_id: UUID | None = None
    #: Why the run is paused or how it ended, in one human-readable sentence.
    detail: str = ""
    head_at_start: str | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class Preference(Base):
    key: str
    value: str
