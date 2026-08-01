"""Guarded worker/job state transitions."""

from __future__ import annotations

from switchboard.domain.enums import ALLOWED_WORKER_TRANSITIONS, WorkerStatus


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
