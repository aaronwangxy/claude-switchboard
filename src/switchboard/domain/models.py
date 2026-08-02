"""Validated domain models persisted in SQLite."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from switchboard.domain.enums import (
    INTAKE_STAGE,
    ArtifactType,
    AttentionKind,
    NativeTurnOrigin,
    NativeTurnStatus,
    RunStatus,
    RuntimeAgentKind,
    RuntimeOwner,
    RuntimeProcessState,
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
    model_config = ConfigDict(populate_by_name=True)

    id: UUID = Field(default_factory=uuid4)
    title: str
    external_ref: str | None = None
    repository_id: UUID
    #: A label the running workflow chose, purely descriptive. It is not a state machine
    #: and nothing gates on it -- `completed_at` is the fact, and it is set only by the
    #: deterministic completion gate.
    stage: str = INTAKE_STAGE
    #: When this job's workflow definition of done was first satisfied.
    completed_at: datetime | None = None
    selected_worker_id: UUID | None = None
    base_ref: str = "main"
    ticket_text: str = ""
    #: The composite workflow this job follows. Stored so a resumed job is reproducible.
    #: `profile` is the name this field had before Phase 10; stored rows still load.
    composite_workflow: str | None = Field(
        default=None, validation_alias=AliasChoices("composite_workflow", "profile")
    )
    #: The one worktree whose Git lineage defines this job's change. Other writable
    #: workers remain isolated, but may not implicitly become the review target.
    authoritative_worktree_id: UUID | None = None
    #: Set when this job exists to serve a larger request. The parent is not complete
    #: until its children are, which is how a decomposed request has one answer.
    parent_job_id: UUID | None = None
    #: Jobs whose stored artifacts are given to this job's workers. This is how one
    #: session's findings reach the next without a person or a model retyping them.
    context_job_ids: list[UUID] = Field(default_factory=list)
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
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class RuntimeInstance(Base):
    """Durable identity and observed state of one substrate-owned agent process."""

    id: UUID = Field(default_factory=uuid4)
    agent_id: UUID
    agent_kind: RuntimeAgentKind = RuntimeAgentKind.WORKER
    generation: int = Field(default=1, ge=1)
    backend: str
    claude_session_id: str | None = None
    process_state: RuntimeProcessState = RuntimeProcessState.STARTING
    owner: RuntimeOwner = RuntimeOwner.MANAGER
    launch_fingerprint: str = ""
    #: Opaque substrate identity. A future backend may store a tmux target here;
    #: orchestration must never interpret these keys.
    substrate: dict[str, str] = Field(default_factory=dict)
    git_head_before_turn: str | None = None
    git_tree_before_turn: str | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class NativeTurn(Base):
    """One turn of a native Claude session, correlated through its lifecycle hooks.

    `origin` is the provenance that decides authority: only a `MANAGED` turn that no
    human touched may harvest artifacts or advance a workflow run.
    """

    id: UUID = Field(default_factory=uuid4)
    runtime_id: UUID
    origin: NativeTurnOrigin
    status: NativeTurnStatus = NativeTurnStatus.PENDING
    correlation_token: str | None = None
    claude_prompt_id: str | None = None
    claude_session_id: str | None = None
    prompt_sha256: str = ""
    human_intervened: bool = False
    final_output: str = ""
    error: str = ""
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class RuntimeHookEvent(Base):
    id: UUID = Field(default_factory=uuid4)
    runtime_id: UUID
    event_name: str
    session_id: str | None = None
    prompt_id: str | None = None
    turn_id: UUID | None = None
    payload: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now)


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
    #: Set only when the current worker's trusted managed turn and all of its artifacts
    #: have been applied durably. A worker assignment alone never means a step finished.
    current_step_completed: bool = False
    completion_turn_id: UUID | None = None
    #: Human ownership or input touched the current step; its automatic completion is
    #: untrusted until the user explicitly reconciles by resuming and replaying it.
    human_intervened: bool = False
    #: Why the run is paused or how it ended, in one human-readable sentence.
    detail: str = ""
    head_at_start: str | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class Preference(Base):
    key: str
    value: str
