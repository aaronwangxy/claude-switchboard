"""The deterministic completion gate.

"Is this work finished?" is answered from stored contracts, stored evidence, and Git --
never from a model's judgement, and never from a worker announcing that it is done. Every
blocker below is a fact somebody can check.

What "finished" *means* is not fixed here. It is derived from the workflow the job is
following: a job is complete when every artifact that workflow's unconditional steps
promise to produce exists, is current for the code, and satisfies the check that artifact
type carries. `complete-ticket` therefore still demands an approved contract, acceptance
criteria, fresh verification, a fresh independent review and a clean tree, while `rebase`
demands a clean tree and fresh verification and says nothing about plans.

A conditional step cannot be a precondition for done -- `address-review-comments` only runs
when there are blocking findings, so requiring its artifact would make a clean change
permanently unfinished. Its evidence is still checked when it exists.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from switchboard.config import Config
from switchboard.core import lineage
from switchboard.core.errors import SessionManagerError
from switchboard.domain.contracts import (
    BehaviorContract,
    CommentResolutionReport,
    ImplementationContract,
    ReviewReport,
    VerificationReport,
)
from switchboard.domain.enums import ArtifactType, RunStatus
from switchboard.domain.models import Artifact
from switchboard.storage.store import Store
from switchboard.workflows.freshness import is_fresh
from switchboard.workflows.registry import find_workflow
from switchboard.workflows.spec import StepCondition, WorkflowDefinition


@dataclass
class CompletionReport:
    """Whether a job's work is finished, and every reason it is not."""

    ready: bool
    blockers: list[str] = field(default_factory=list)
    blurb: str = ""
    #: The workflow whose definition of done was applied, for explainability.
    workflow: str | None = None
    #: The artifact types that workflow requires, in a stable order.
    required: list[str] = field(default_factory=list)

    def explain(self) -> str:
        """One paragraph a person or the Manager can read without further lookup."""
        standard = (
            f"{self.workflow}'s definition of done" if self.workflow else "the job's own evidence"
        )
        if self.ready:
            return f"Complete against {standard}."
        return f"Not complete against {standard}: " + " ".join(self.blockers)


# --------------------------------------------------------- per-artifact checks


def _contract_blockers(artifact: Artifact, config: Config) -> list[str]:
    contract = ImplementationContract.model_validate(artifact.body)
    blockers: list[str] = []
    if config.commits.require_plan and not contract.approved:
        blockers.append("The implementation contract has not been approved.")
    if contract.blocking_decisions():
        blockers.append(f"{len(contract.blocking_decisions())} blocking decision(s) unanswered.")
    return blockers


def _behavior_blockers(artifact: Artifact, config: Config) -> list[str]:
    criteria = BehaviorContract.model_validate(artifact.body).criteria
    if not criteria:
        return ["No acceptance criteria recorded."]
    return [
        f"Criterion {criterion.id} is {criterion.status}."
        for criterion in criteria
        if criterion.status != "passed" and not criterion.accepted_limitation
    ]


def _verification_blockers(artifact: Artifact, config: Config) -> list[str]:
    report = VerificationReport.model_validate(artifact.body)
    return [] if report.passed else ["Verification did not pass."]


def _review_blockers(artifact: Artifact, config: Config) -> list[str]:
    unresolved = ReviewReport.model_validate(artifact.body).unresolved_blocking(
        set(config.workflows.review_change.blocking_severities)
    )
    return [f"{len(unresolved)} unresolved blocking review finding(s)."] if unresolved else []


def _resolution_blockers(artifact: Artifact, config: Config) -> list[str]:
    report = CommentResolutionReport.model_validate(artifact.body)
    pending = [r for r in report.resolutions if r.classification == "needs_human_decision"]
    return [f"{len(pending)} review comment(s) still need a human decision."] if pending else []


#: The semantic check each artifact type carries. Adding an artifact type means adding its
#: Pydantic schema in `domain/contracts.py` and, if it can be unsatisfied, its check here.
#: A type with no entry only has to exist and be current.
COMPLETION_CHECKS: dict[ArtifactType, Callable[[Artifact, Config], list[str]]] = {
    ArtifactType.IMPLEMENTATION_CONTRACT: _contract_blockers,
    ArtifactType.BEHAVIOR_CONTRACT: _behavior_blockers,
    ArtifactType.VERIFICATION: _verification_blockers,
    ArtifactType.SMOKE_VERIFICATION: _verification_blockers,
    ArtifactType.REVIEW: _review_blockers,
    ArtifactType.COMMENT_RESOLUTIONS: _resolution_blockers,
}


# ------------------------------------------------------- what a workflow means


def required_artifacts(definition: WorkflowDefinition | None) -> frozenset[ArtifactType]:
    """The artifacts a workflow promises unconditionally, so they may be demanded."""
    if definition is None:
        return frozenset()
    if not definition.is_composite:
        return definition.produces
    required: set[ArtifactType] = set()
    for step in definition.steps:
        if step.when is not StepCondition.ALWAYS:
            continue
        step_definition = find_workflow(step.workflow)
        if step_definition is not None:
            required |= step_definition.produces
    return frozenset(required)


def touches_code(definition: WorkflowDefinition | None) -> bool:
    """Whether any part of this workflow may write to the repository."""
    if definition is None:
        return False
    if not definition.is_composite:
        return definition.mutates_code
    return any(
        step_definition.mutates_code
        for step in definition.steps
        if (step_definition := find_workflow(step.workflow)) is not None
    )


# ------------------------------------------------------------------- the gate


def job_completion(store: Store, config: Config, job_id: UUID) -> CompletionReport:
    """Every reason this job's work is not finished, or an empty list."""
    job = store.get_job(job_id)
    if job is None:
        raise SessionManagerError(f"Job {job_id} does not exist.")
    definition = find_workflow(job.composite_workflow)
    if definition is None:
        # Nothing has said what finished means for this job, and an empty checklist is
        # not the same as a satisfied one. Reporting `ready` here would let a brand new
        # job -- or one whose workflow file was deleted -- be called complete.
        return CompletionReport(
            ready=False,
            blockers=[
                "This job is not following a workflow, so nothing has defined what "
                "finished means for it."
            ],
            blurb=verification_blurb(store, job_id),
        )
    required = required_artifacts(definition)

    blockers: list[str] = []
    writes = touches_code(definition) or job.authoritative_worktree_id is not None
    if writes and job.authoritative_worktree_id is None:
        blockers.append("No authoritative change worktree is selected.")

    head, dirty = lineage.job_head_and_dirty(store, job)
    for type_ in sorted(required, key=lambda t: t.value):
        artifact = store.latest_artifact(job_id, type_)
        if artifact is None:
            blockers.append(f"No {type_.value.replace('_', ' ')}.")
            continue
        if artifact.stale or (head and not is_fresh(artifact, head)):
            blockers.append(
                f"The {type_.value.replace('_', ' ')} does not apply to current HEAD."
            )
            continue
        check = COMPLETION_CHECKS.get(type_)
        if check is not None:
            blockers.extend(check(artifact, config))

    run = store.active_run(job_id)
    if run is not None and run.status is not RunStatus.COMPLETED:
        total = len(definition.steps) if definition and definition.is_composite else 0
        position = f"step {run.step_index + 1}" + (f" of {total}" if total else "")
        blockers.append(f"Its {run.workflow} run is still {run.status.value} at {position}.")

    if dirty:
        blockers.append(f"The worktree has {len(dirty)} uncommitted change(s).")

    return CompletionReport(
        ready=not blockers,
        blockers=blockers,
        blurb=verification_blurb(store, job_id),
        workflow=definition.name if definition else None,
        required=sorted(t.value for t in required),
    )


def verification_blurb(store: Store, job_id: UUID) -> str:
    """A copy-pastable summary built only from stored evidence -- never from memory."""
    verification = store.latest_artifact(job_id, ArtifactType.VERIFICATION) or store.latest_artifact(
        job_id, ArtifactType.SMOKE_VERIFICATION
    )
    review = store.latest_artifact(job_id, ArtifactType.REVIEW)
    lines = ["Verification performed:"]
    limitations: list[str] = []
    if verification is None:
        lines.append("- None recorded.")
    else:
        report = VerificationReport.model_validate(verification.body)
        for evidence in report.evidence:
            commands = ", ".join(
                f"`{c.command}` (exit {c.exit_code})" for c in evidence.commands
            )
            lines.append(
                f"- {evidence.criterion_id}: {evidence.status} — {evidence.observed_behavior}"
                + (f" [{commands}]" if commands else "")
            )
            limitations.extend(evidence.limitations)
        if report.tested_head:
            lines.append(f"- Tested head: {report.tested_head[:12]}")
    if review is not None:
        parsed = ReviewReport.model_validate(review.body)
        open_findings = [f for f in parsed.findings if not f.resolved]
        lines.append(
            f"- Independent review of {parsed.reviewed_head[:12]}: {parsed.verdict}, "
            f"{len(open_findings)} open finding(s)."
        )
    lines.append("")
    lines.append("Limitations:")
    lines.extend(f"- {item}" for item in (limitations or ["None recorded."]))
    return "\n".join(lines)
