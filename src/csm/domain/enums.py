"""Enumerations for the core domain."""

from __future__ import annotations

from enum import Enum


class WorkerStatus(str, Enum):
    STARTING = "starting"
    WORKING = "working"
    BLOCKED = "blocked"
    IDLE = "idle"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"
    DISCONNECTED = "disconnected"


TERMINAL_WORKER_STATUSES = frozenset(
    {WorkerStatus.DONE, WorkerStatus.FAILED, WorkerStatus.STOPPED, WorkerStatus.DISCONNECTED}
)

#: Only these transitions are permitted. Enforced in `csm.core.transitions`.
ALLOWED_WORKER_TRANSITIONS: dict[WorkerStatus, frozenset[WorkerStatus]] = {
    WorkerStatus.STARTING: frozenset(
        {WorkerStatus.WORKING, WorkerStatus.FAILED, WorkerStatus.STOPPED, WorkerStatus.IDLE}
    ),
    WorkerStatus.WORKING: frozenset(
        {
            WorkerStatus.BLOCKED,
            WorkerStatus.IDLE,
            WorkerStatus.DONE,
            WorkerStatus.FAILED,
            WorkerStatus.STOPPED,
            WorkerStatus.DISCONNECTED,
        }
    ),
    WorkerStatus.BLOCKED: frozenset(
        {
            WorkerStatus.WORKING,
            WorkerStatus.IDLE,
            WorkerStatus.FAILED,
            WorkerStatus.STOPPED,
            WorkerStatus.DISCONNECTED,
        }
    ),
    WorkerStatus.IDLE: frozenset(
        {
            WorkerStatus.WORKING,
            WorkerStatus.BLOCKED,
            WorkerStatus.DONE,
            WorkerStatus.FAILED,
            WorkerStatus.STOPPED,
            WorkerStatus.DISCONNECTED,
        }
    ),
    WorkerStatus.DONE: frozenset({WorkerStatus.WORKING, WorkerStatus.STOPPED}),
    WorkerStatus.FAILED: frozenset({WorkerStatus.WORKING, WorkerStatus.STOPPED}),
    WorkerStatus.DISCONNECTED: frozenset({WorkerStatus.STOPPED, WorkerStatus.WORKING}),
    WorkerStatus.STOPPED: frozenset(),
}


class JobStage(str, Enum):
    INTAKE = "intake"
    PLANNING = "planning"
    AWAITING_INPUT = "awaiting_input"
    IMPLEMENTING = "implementing"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    FIXING = "fixing"
    READY_TO_PUSH = "ready_to_push"
    COMPLETED = "completed"
    FAILED = "failed"


class RunStatus(str, Enum):
    """Where a composite workflow run stands. Persisted, so a run survives a restart."""

    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED})


class WorkerRole(str, Enum):
    GENERAL = "general"
    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    VERIFIER = "verifier"
    REVIEWER = "reviewer"
    QUESTION = "question"
    REBASE = "rebase"
    REVIEW_COMMENTS = "review_comments"


#: Roles that are read-only by default and therefore need no worktree of their own.
READ_ONLY_ROLES = frozenset(
    {WorkerRole.PLANNER, WorkerRole.REVIEWER, WorkerRole.QUESTION, WorkerRole.VERIFIER}
)


class ArtifactType(str, Enum):
    IMPLEMENTATION_CONTRACT = "implementation_contract"
    BEHAVIOR_CONTRACT = "behavior_contract"
    EVIDENCE_CONTRACT = "evidence_contract"
    VERIFICATION = "verification"
    SMOKE_VERIFICATION = "smoke_verification"
    REVIEW = "review"
    COMMENT_RESOLUTIONS = "comment_resolutions"


class AttentionKind(str, Enum):
    """Attention reasons, most urgent first. Ordinal drives queue priority."""

    HUMAN_DECISION = "human_decision"
    PERMISSION_REQUIRED = "permission_required"
    WORKER_FAILED = "worker_failed"
    PLAN_APPROVAL = "plan_approval"
    BLOCKING_REVIEW_FINDING = "blocking_review_finding"
    VERIFICATION_FAILED = "verification_failed"
    READY_FOR_REVIEW = "ready_for_review"
    READY_TO_PUSH = "ready_to_push"
    CLEANUP_CANDIDATE = "cleanup_candidate"


#: Section 4.3 priority order; lower number sorts first.
ATTENTION_PRIORITY: dict[AttentionKind, int] = {
    kind: index for index, kind in enumerate(AttentionKind)
}


class Verbosity(str, Enum):
    CONCISE = "concise"
    NORMAL = "normal"
    DETAILED = "detailed"
