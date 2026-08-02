"""The deterministic ready-to-push gate.

"Is this change finished?" is answered from stored contracts, stored evidence, and Git --
never from a model's judgement, and never from a worker announcing that it is done. Every
blocker below is a fact somebody can check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from switchboard.config import Config
from switchboard.core import lineage
from switchboard.core.errors import SessionManagerError
from switchboard.domain.contracts import (
    BehaviorContract,
    ImplementationContract,
    ReviewReport,
    VerificationReport,
)
from switchboard.domain.enums import ArtifactType
from switchboard.storage.store import Store
from switchboard.workflows.freshness import is_fresh


@dataclass
class ReadyToPushReport:
    ready: bool
    blockers: list[str] = field(default_factory=list)
    blurb: str = ""


def ready_to_push(store: Store, config: Config, job_id: UUID) -> ReadyToPushReport:
    """Every reason this change is not finished, or an empty list."""
    job = store.get_job(job_id)
    if job is None:
        raise SessionManagerError(f"Job {job_id} does not exist.")
    blockers: list[str] = []
    if job.authoritative_worktree_id is None:
        blockers.append("No authoritative change worktree is selected.")

    contract_artifact = store.latest_artifact(job_id, ArtifactType.IMPLEMENTATION_CONTRACT)
    if contract_artifact is None:
        blockers.append("No implementation contract.")
    else:
        contract = ImplementationContract.model_validate(contract_artifact.body)
        if not contract.approved:
            blockers.append("The implementation contract has not been approved.")
        if contract.blocking_decisions():
            blockers.append(
                f"{len(contract.blocking_decisions())} blocking decision(s) unanswered."
            )

    behavior_artifact = store.latest_artifact(job_id, ArtifactType.BEHAVIOR_CONTRACT)
    criteria = (
        BehaviorContract.model_validate(behavior_artifact.body).criteria
        if behavior_artifact
        else []
    )
    if not criteria:
        blockers.append("No acceptance criteria recorded.")
    for criterion in criteria:
        if criterion.status != "passed" and not criterion.accepted_limitation:
            blockers.append(f"Criterion {criterion.id} is {criterion.status}.")

    head, dirty = lineage.job_head_and_dirty(store, job)
    verification = store.latest_artifact(job_id, ArtifactType.VERIFICATION)
    if verification is None:
        blockers.append("No verification evidence.")
    elif verification.stale or (head and not is_fresh(verification, head)):
        blockers.append("Verification does not apply to current HEAD.")

    review = store.latest_artifact(job_id, ArtifactType.REVIEW)
    if review is None:
        blockers.append("No independent review.")
    elif review.stale or (head and not is_fresh(review, head)):
        blockers.append("Review does not apply to current HEAD.")
    else:
        unresolved = ReviewReport.model_validate(review.body).unresolved_blocking(
            set(config.workflows.review_change.blocking_severities)
        )
        if unresolved:
            blockers.append(f"{len(unresolved)} unresolved blocking review finding(s).")

    if dirty:
        blockers.append(f"The worktree has {len(dirty)} uncommitted change(s).")

    return ReadyToPushReport(
        ready=not blockers, blockers=blockers, blurb=verification_blurb(store, job_id)
    )


def verification_blurb(store: Store, job_id: UUID) -> str:
    """A copy-pastable summary built only from stored evidence -- never from memory."""
    verification = store.latest_artifact(job_id, ArtifactType.VERIFICATION)
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
