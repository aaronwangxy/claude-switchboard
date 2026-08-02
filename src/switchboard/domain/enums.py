"""Enumerations for the core domain.

Everything here is closed except `WorkerRole`, and that exception is the point: a role is
something a *workflow* declares, so adding a workflow must not require editing this file.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, ClassVar

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema


class WorkerStatus(str, Enum):
    STARTING = "starting"
    WORKING = "working"
    BLOCKED = "blocked"
    IDLE = "idle"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"
    DISCONNECTED = "disconnected"


class RuntimeProcessState(str, Enum):
    """Substrate-neutral lifecycle of one agent process instance."""

    ABSENT = "absent"
    STARTING = "starting"
    READY = "ready"
    TURN_ACTIVE = "turn_active"
    WAITING = "waiting"
    TURN_COMPLETE = "turn_complete"
    EXITED = "exited"


class RuntimeOwner(str, Enum):
    """Who is currently allowed to drive the runtime's input."""

    MANAGER = "manager"
    HUMAN = "human"


class RuntimeAgentKind(str, Enum):
    WORKER = "worker"
    MANAGER = "manager"


class NativeTurnOrigin(str, Enum):
    MANAGED = "managed"
    HUMAN = "human"


class NativeTurnStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    WAITING_PERMISSION = "waiting_permission"
    INTERRUPT_REQUESTED = "interrupt_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


TERMINAL_WORKER_STATUSES = frozenset(
    {WorkerStatus.DONE, WorkerStatus.FAILED, WorkerStatus.STOPPED, WorkerStatus.DISCONNECTED}
)

#: Only these transitions are permitted. Enforced in `switchboard.core.transitions`.
ALLOWED_WORKER_TRANSITIONS: dict[WorkerStatus, frozenset[WorkerStatus]] = {
    WorkerStatus.STARTING: frozenset(
        {
            WorkerStatus.WORKING,
            WorkerStatus.BLOCKED,
            WorkerStatus.FAILED,
            WorkerStatus.STOPPED,
            WorkerStatus.IDLE,
        }
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


_ROLE_NAME = re.compile(r"[a-z][a-z0-9]*([_-][a-z0-9]+)*")


class WorkerRole(str):
    """The name of the part a worker plays in a workflow.

    Deliberately a validated string rather than an enum. `plan-feature` needs a planner and
    `investigate` needs an investigator, and both are ordinary YAML files -- if the second
    one required a new enum member, the first workflow would be the architecture and the
    second would be an extension of it. The constants below are the roles the built-ins
    happen to use, not the permitted set.

    `.value` is kept so call sites read the same as they did against the enum.
    """

    __slots__ = ()

    #: One instance per name, so `WorkerRole("verifier") is WorkerRole.VERIFIER` holds and
    #: identity comparisons behave exactly as they did against the enum this replaced.
    _interned: ClassVar[dict[str, WorkerRole]] = {}

    GENERAL: ClassVar[WorkerRole]
    PLANNER: ClassVar[WorkerRole]
    IMPLEMENTER: ClassVar[WorkerRole]
    VERIFIER: ClassVar[WorkerRole]
    REVIEWER: ClassVar[WorkerRole]
    QUESTION: ClassVar[WorkerRole]
    REBASE: ClassVar[WorkerRole]
    REVIEW_COMMENTS: ClassVar[WorkerRole]
    INVESTIGATOR: ClassVar[WorkerRole]

    def __new__(cls, value: str) -> WorkerRole:
        name = str(value).strip().lower()
        existing = cls._interned.get(name)
        if existing is not None:
            return existing
        if not _ROLE_NAME.fullmatch(name):
            raise ValueError(
                f"{value!r} is not a usable role name. Use lowercase words joined by '-' "
                "or '_', such as 'investigator' or 'review_comments'."
            )
        role = super().__new__(cls, name)
        cls._interned[name] = role
        return role

    @property
    def value(self) -> str:
        return str(self)

    def __repr__(self) -> str:
        return f"WorkerRole({str(self)!r})"

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        # Validate through `__new__` so a stored or YAML-authored role is checked once,
        # and serialise as the plain name so nothing in the database changes shape.
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(),
            serialization=core_schema.plain_serializer_function_ser_schema(
                str, return_schema=core_schema.str_schema()
            ),
        )


for _name in (
    "GENERAL",
    "PLANNER",
    "IMPLEMENTER",
    "VERIFIER",
    "REVIEWER",
    "QUESTION",
    "REBASE",
    "REVIEW_COMMENTS",
    "INVESTIGATOR",
):
    setattr(WorkerRole, _name, WorkerRole(_name.lower()))
del _name


#: A bare `create_worker` with no workflow gets a worktree only for these roles. Every
#: workflow-started worker takes its writability from the workflow's `mutates_code`
#: instead, so a role nobody here anticipated is read-only rather than unconstrained.
DEFAULT_WRITABLE_ROLES = frozenset(
    {
        WorkerRole.GENERAL,
        WorkerRole.IMPLEMENTER,
        WorkerRole.REBASE,
        WorkerRole.REVIEW_COMMENTS,
    }
)


class ArtifactType(str, Enum):
    IMPLEMENTATION_CONTRACT = "implementation_contract"
    BEHAVIOR_CONTRACT = "behavior_contract"
    VERIFICATION = "verification"
    SMOKE_VERIFICATION = "smoke_verification"
    REVIEW = "review"
    COMMENT_RESOLUTIONS = "comment_resolutions"
    WORKFLOW_PROPOSALS = "workflow_proposals"


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


#: Lower number sorts first. The enum's declaration order *is* the priority.
ATTENTION_PRIORITY: dict[AttentionKind, int] = {
    kind: index for index, kind in enumerate(AttentionKind)
}


class Verbosity(str, Enum):
    CONCISE = "concise"
    NORMAL = "normal"
    DETAILED = "detailed"
