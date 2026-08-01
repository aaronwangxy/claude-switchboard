"""Composite workflow step conditions.

Every condition is answered from stored state and Git lineage -- never from a model
remembering that something changed. That is what makes the ritual reproducible: a run
resumed tomorrow evaluates its next step exactly as it would have today.
"""

from __future__ import annotations

from csm.config import Config
from csm.domain.contracts import ImplementationContract, ReviewReport, VerificationReport
from csm.domain.enums import ArtifactType
from csm.domain.models import Job, WorkflowRun
from csm.storage.store import Store
from csm.workflows.freshness import is_fresh
from csm.workflows.spec import StepCondition, WorkflowDefinition


def _current(store: Store, job: Job, type_: ArtifactType, head: str | None):
    """The latest artifact of this type, or None when it does not apply to current code."""
    artifact = store.latest_artifact(job.id, type_)
    if artifact is None or artifact.stale:
        return None
    if head and not is_fresh(artifact, head):
        return None
    return artifact


def has_blocking_decisions(store: Store, job: Job) -> bool:
    artifact = store.latest_artifact(job.id, ArtifactType.IMPLEMENTATION_CONTRACT)
    if artifact is None:
        return False
    return bool(ImplementationContract.model_validate(artifact.body).blocking_decisions())


def has_blocking_findings(store: Store, job: Job, config: Config, head: str | None) -> bool:
    artifact = _current(store, job, ArtifactType.REVIEW, head)
    if artifact is None:
        return False
    report = ReviewReport.model_validate(artifact.body)
    severities = set(config.workflows.review_change.blocking_severities)
    return bool(report.unresolved_blocking(severities))


def verification_failed(store: Store, job: Job, head: str | None) -> bool:
    artifact = _current(store, job, ArtifactType.VERIFICATION, head)
    if artifact is None:
        return False
    return not VerificationReport.model_validate(artifact.body).passed


def evidence_is_stale(store: Store, job: Job, definition: WorkflowDefinition, head: str | None) -> bool:
    """True when what this step produces is missing or no longer applies to current code.

    This is what `when: code-changed` means in practice: rerun the step whose evidence the
    code has moved past. A step that produces nothing falls back to comparing HEAD.
    """
    if not definition.produces:
        return False
    return any(_current(store, job, type_, head) is None for type_ in definition.produces)


def condition_holds(
    condition: StepCondition,
    *,
    store: Store,
    config: Config,
    job: Job,
    run: WorkflowRun,
    definition: WorkflowDefinition,
    head: str | None,
) -> bool:
    match condition:
        case StepCondition.ALWAYS:
            return True
        case StepCondition.HUMAN_DECISIONS:
            return has_blocking_decisions(store, job)
        case StepCondition.BLOCKING_FINDINGS:
            return has_blocking_findings(store, job, config, head)
        case StepCondition.VERIFICATION_FAILED:
            return verification_failed(store, job, head)
        case StepCondition.CODE_CHANGED:
            if definition.produces:
                return evidence_is_stale(store, job, definition, head)
            return bool(head) and head != run.head_at_start
    return False
