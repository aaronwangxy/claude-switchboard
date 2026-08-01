"""The declarative workflow model.

A workflow is either *atomic* -- a prompt template run by one independent Claude worker
-- or *composite* -- an ordered list of steps that reference other workflows. Composite
workflows are how the development ritual itself becomes configurable: `complete-ticket`
is a composite of the same atomic workflows a user can invoke directly.

Both kinds are the same type, loaded from the same YAML, whether they ship with Switchboard or
live in `~/.switchboard/workflows`. There is deliberately no graph, no branching and no
expression language: a sequence, five named conditions, and a bounded repeat count.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from switchboard.domain.enums import ArtifactType, JobStage, WorkerRole

#: Only `{name}` tokens we actually supply are substituted; every other brace -- notably
#: the JSON schema braces in prompt templates -- is left alone. This is why workflow
#: authors never have to escape `{{`.
_TOKEN = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def render_template(template: str, values: dict[str, object]) -> str:
    """Substitute the known `{token}`s in a prompt template, leaving all other braces."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(values[key]) if key in values else match.group(0)

    return _TOKEN.sub(replace, template or "")


class StepCondition(str, Enum):
    """When a composite step runs. Every condition is computed from stored state."""

    ALWAYS = "always"
    HUMAN_DECISIONS = "human-decisions"
    CODE_CHANGED = "code-changed"
    VERIFICATION_FAILED = "verification-failed"
    BLOCKING_FINDINGS = "blocking-findings"


class Approval(str, Enum):
    """Whether a composite pauses for the user after a step produces its artifact."""

    NONE = "none"
    REQUIRED = "required"
    ONLY_IF_DECISIONS = "only-if-decisions"


class WorkerMode(str, Enum):
    """Which worker a step runs on."""

    #: A brand new independent session that inherits no sibling's transcript.
    FRESH = "fresh"
    #: Reuse the job's existing worker for this role when there is one.
    EXISTING = "existing"
    #: Let `SessionManager` decide from the workflow's role and writability.
    AUTO = "auto"


class WorkflowStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    workflow: str
    when: StepCondition = StepCondition.ALWAYS
    approval: Approval = Approval.NONE
    worker: WorkerMode = WorkerMode.AUTO
    #: Bounded repeat: how many times this step may run in one composite run.
    max_iterations: int = Field(default=1, ge=1, le=10)


class WorkflowDefinition(BaseModel):
    """One workflow, loaded from YAML.

    Atomic workflows carry a `prompt`; composite workflows carry `steps`. The safety
    fields (`allowed_roles`, `mutates_code`, `requires`) are enforced by the session
    manager and cannot be used to weaken a safety invariant -- a workflow can only ask
    for less than the application already permits, never more.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    role: WorkerRole = WorkerRole.GENERAL
    allowed_roles: frozenset[WorkerRole] = frozenset()
    mutates_code: bool = False
    #: Artifacts that must exist and be current before this workflow may run.
    requires: frozenset[ArtifactType] = frozenset()
    produces: frozenset[ArtifactType] = frozenset()
    invalidates: frozenset[ArtifactType] = frozenset()
    #: Extra stored artifacts to put in the worker's prompt beyond `requires`.
    context: frozenset[ArtifactType] = frozenset()
    #: The job stage this workflow moves its job to when it starts.
    stage: JobStage | None = None
    model_role: str = "general"
    worker: WorkerMode = WorkerMode.AUTO
    prompt: str = ""
    steps: tuple[WorkflowStep, ...] = ()
    #: Set by the loader: where this definition came from, for `sb workflows`.
    source: str = "builtin"

    @model_validator(mode="after")
    def _check(self) -> WorkflowDefinition:
        if not self.name:
            raise ValueError("A workflow needs a name.")
        if self.steps and self.prompt:
            raise ValueError(
                f"Workflow {self.name!r} has both steps and a prompt; it must be one or the other."
            )
        if not self.steps and not self.prompt:
            raise ValueError(f"Workflow {self.name!r} needs either a prompt or steps.")
        if not self.allowed_roles and not self.steps:
            object.__setattr__(self, "allowed_roles", frozenset({self.role}))
        return self

    @property
    def is_composite(self) -> bool:
        return bool(self.steps)

    # Names the rest of the application reads. Keeping them as properties means the
    # YAML stays readable without every call site learning two vocabularies.
    @property
    def required_artifacts(self) -> frozenset[ArtifactType]:
        return self.requires

    @property
    def produced_artifacts(self) -> frozenset[ArtifactType]:
        return self.produces

    @property
    def default_role(self) -> WorkerRole:
        return self.role

    @property
    def default_model_role(self) -> str:
        return self.model_role

    @property
    def template(self) -> str:
        return self.prompt

    @property
    def prompt_context(self) -> frozenset[ArtifactType]:
        """Every stored artifact type this workflow's prompt should be given."""
        return self.requires | self.context

    @property
    def scope(self) -> str:
        """Verification scope, derived from what the workflow produces."""
        return "smoke" if ArtifactType.SMOKE_VERIFICATION in self.produces else "full"


class WorkflowError(ValueError):
    """A workflow was requested that does not exist or is not allowed for the role."""
