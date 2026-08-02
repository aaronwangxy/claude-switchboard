"""Only the transitions the state machine permits."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from switchboard.core.transitions import TransitionError, assert_worker_transition
from switchboard.domain.enums import WorkerStatus

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------ transitions


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (WorkerStatus.STARTING, WorkerStatus.WORKING),
        (WorkerStatus.STARTING, WorkerStatus.BLOCKED),
        (WorkerStatus.WORKING, WorkerStatus.BLOCKED),
        (WorkerStatus.BLOCKED, WorkerStatus.WORKING),
        (WorkerStatus.IDLE, WorkerStatus.WORKING),
        (WorkerStatus.WORKING, WorkerStatus.DISCONNECTED),
        (WorkerStatus.DISCONNECTED, WorkerStatus.STOPPED),
    ],
)
def test_permitted_transitions(current, target):
    assert_worker_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (WorkerStatus.STOPPED, WorkerStatus.WORKING),
        (WorkerStatus.STOPPED, WorkerStatus.IDLE),
        (WorkerStatus.DONE, WorkerStatus.BLOCKED),
    ],
)
def test_refused_transitions(current, target):
    with pytest.raises(TransitionError):
        assert_worker_transition(current, target)


def test_a_transition_to_the_same_status_is_a_no_op():
    assert_worker_transition(WorkerStatus.STOPPED, WorkerStatus.STOPPED)


def test_refusal_names_the_permitted_targets():
    with pytest.raises(TransitionError, match="Allowed: none"):
        assert_worker_transition(WorkerStatus.STOPPED, WorkerStatus.WORKING)
