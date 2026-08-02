"""Canonical event kinds. Events drive persistence, status, and the attention queue."""

from __future__ import annotations

WORKER_CREATED = "worker.created"
WORKER_STARTED = "worker.started"
WORKER_OUTPUT = "worker.output"
WORKER_BLOCKED = "worker.blocked"
WORKER_PERMISSION_REQUIRED = "worker.permission_required"
WORKER_RESUMED = "worker.resumed"
WORKER_FAILED = "worker.failed"
WORKER_COMPLETED = "worker.completed"
WORKER_STOPPED = "worker.stopped"
#: The user took direct control of a worker's session in their own terminal.
WORKER_ATTACHED = "worker.attached"
PLAN_CREATED = "plan.created"
PLAN_REQUIRES_INPUT = "plan.requires_input"
PLAN_APPROVED = "plan.approved"
#: A question or investigation left a durable answer rather than only a transcript.
FINDINGS_RECORDED = "findings.recorded"
VERIFICATION_STARTED = "verification.started"
VERIFICATION_PASSED = "verification.passed"
VERIFICATION_FAILED = "verification.failed"
REVIEW_STARTED = "review.started"
REVIEW_BLOCKING_FINDINGS = "review.blocking_findings"
REVIEW_PASSED = "review.passed"
RUN_STARTED = "run.started"
RUN_PAUSED = "run.paused"
RUN_COMPLETED = "run.completed"
#: The job's workflow definition of done is satisfied, for the first time.
JOB_COMPLETE = "job.complete"
#: A native workspace-trust prompt was answered under recorded per-repository consent.
WORKSPACE_TRUSTED = "workspace.trusted"
ARTIFACT_INVALIDATED = "artifact.invalidated"
CLEANUP_REFUSED = "cleanup.refused"
CLEANUP_COMPLETED = "cleanup.completed"
#: The user turned a mined proposal into a real workflow.
WORKFLOW_PROPOSAL_ACCEPTED = "workflow.proposal_accepted"

ALL_EVENT_KINDS = frozenset(
    v for k, v in list(globals().items()) if k.isupper() and isinstance(v, str) and "." in v
)
