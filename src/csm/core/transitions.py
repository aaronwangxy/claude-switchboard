"""Guarded worker/job state transitions."""

from __future__ import annotations

from csm.domain.enums import ALLOWED_WORKER_TRANSITIONS, JobStage, WorkerStatus


class TransitionError(ValueError):
    """A state transition that the state machine does not permit."""


def assert_worker_transition(current: WorkerStatus, target: WorkerStatus) -> None:
    if current == target:
        return
    allowed = ALLOWED_WORKER_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise TransitionError(
            f"Cannot move a worker from {current.value} to {target.value}. "
            f"Allowed: {', '.join(sorted(s.value for s in allowed)) or 'none'}."
        )


#: The stage a job moves to when a workflow starts on it.
WORKFLOW_STAGE: dict[str, JobStage] = {
    "plan-feature": JobStage.PLANNING,
    "implement-approved-plan": JobStage.IMPLEMENTING,
    "address-review-comments": JobStage.FIXING,
    "rebase-stack": JobStage.IMPLEMENTING,
    "restack-commits": JobStage.IMPLEMENTING,
    "smoke-test": JobStage.VERIFYING,
    "full-verify": JobStage.VERIFYING,
    "review-change": JobStage.REVIEWING,
    "rereview": JobStage.REVIEWING,
    "finalize-change": JobStage.READY_TO_PUSH,
}
