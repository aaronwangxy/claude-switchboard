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
PLAN_CREATED = "plan.created"
PLAN_REQUIRES_INPUT = "plan.requires_input"
PLAN_APPROVED = "plan.approved"
VERIFICATION_STARTED = "verification.started"
VERIFICATION_PASSED = "verification.passed"
VERIFICATION_FAILED = "verification.failed"
REVIEW_STARTED = "review.started"
REVIEW_BLOCKING_FINDINGS = "review.blocking_findings"
REVIEW_PASSED = "review.passed"
RUN_STARTED = "run.started"
RUN_PAUSED = "run.paused"
RUN_COMPLETED = "run.completed"
JOB_READY_TO_PUSH = "job.ready_to_push"
ARTIFACT_INVALIDATED = "artifact.invalidated"
CLEANUP_REFUSED = "cleanup.refused"
CLEANUP_COMPLETED = "cleanup.completed"

ALL_EVENT_KINDS = frozenset(
    v for k, v in list(globals().items()) if k.isupper() and isinstance(v, str) and "." in v
)
