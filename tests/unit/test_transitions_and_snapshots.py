"""Worker state-transition guards and manager snapshot bounding."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from switchboard.agents.snapshots import (
    MAX_EVENTS,
    MAX_EXCHANGES,
    MAX_WORKERS_IN_DETAIL,
    Exchange,
    SnapshotInput,
    build_snapshot,
)
from switchboard.core.transitions import TransitionError, assert_worker_transition
from switchboard.domain.enums import AttentionKind, JobStage, WorkerRole, WorkerStatus
from switchboard.domain.models import AttentionItem, Event, Job, Repository, Worker

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------ transitions


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (WorkerStatus.STARTING, WorkerStatus.WORKING),
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
        (WorkerStatus.STARTING, WorkerStatus.BLOCKED),
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


# -------------------------------------------------------------------- snapshots


@pytest.fixture
def repo() -> Repository:
    return Repository(name="alpha", root_path=Path("/tmp/alpha"))


def make_worker(repo: Repository, index: int, status: WorkerStatus) -> Worker:
    return Worker(
        title=f"worker-{index}",
        role=WorkerRole.IMPLEMENTER,
        status=status,
        repository_id=repo.id,
        cwd=repo.root_path,
    )


def build(repo: Repository, **kwargs) -> str:
    data = SnapshotInput(
        repositories=[repo],
        jobs=kwargs.pop("jobs", []),
        workers=kwargs.pop("workers", []),
        attention=kwargs.pop("attention", []),
        events=kwargs.pop("events", []),
        **kwargs,
    )
    return build_snapshot(data)


def test_workers_beyond_the_detail_bound_are_summarised_by_status_count(repo):
    workers = [make_worker(repo, i, WorkerStatus.WORKING) for i in range(MAX_WORKERS_IN_DETAIL + 4)]
    snapshot = build(repo, workers=workers)
    detailed = [line for line in snapshot.splitlines() if "title='worker-" in line]
    assert len(detailed) == MAX_WORKERS_IN_DETAIL
    assert "plus 4 working" in snapshot


def test_inactive_workers_are_summarised_rather_than_detailed(repo):
    workers = [make_worker(repo, 0, WorkerStatus.WORKING)]
    workers += [make_worker(repo, i, WorkerStatus.STOPPED) for i in range(1, 4)]
    snapshot = build(repo, workers=workers)
    assert "worker-0" in snapshot
    assert "worker-2" not in snapshot
    assert "plus 3 stopped" in snapshot


def test_recent_events_are_capped(repo):
    events = [Event(kind="worker.output", summary=f"event-{i}") for i in range(MAX_EVENTS + 5)]
    snapshot = build(repo, events=events)
    assert "event-0" in snapshot
    assert f"event-{MAX_EVENTS}" not in snapshot


def test_manager_exchanges_are_capped_to_a_recent_window(repo):
    exchanges = [Exchange(user=f"u{i}", manager=f"m{i}") for i in range(MAX_EXCHANGES + 5)]
    snapshot = build(repo, exchanges=exchanges)
    assert "u0" not in snapshot
    assert f"u{MAX_EXCHANGES + 4}" in snapshot


def test_completed_jobs_are_excluded_unless_explicitly_referenced(repo):
    done = Job(title="finished work", repository_id=repo.id, stage=JobStage.COMPLETED)
    live = Job(title="live work", repository_id=repo.id, stage=JobStage.IMPLEMENTING)
    assert "finished work" not in build(repo, jobs=[done, live])
    assert "live work" in build(repo, jobs=[done, live])
    assert "finished work" in build(repo, jobs=[done, live], referenced_job_ids={done.id})


def test_snapshot_never_contains_worker_transcripts(repo):
    worker = make_worker(repo, 0, WorkerStatus.WORKING)
    snapshot = build(repo, workers=[worker])
    assert "transcript" not in snapshot.lower()
    assert "## Available workflows" in snapshot


def test_attention_items_appear_in_the_order_given(repo):
    worker = make_worker(repo, 0, WorkerStatus.BLOCKED)
    items = [
        AttentionItem(worker_id=worker.id, kind=AttentionKind.HUMAN_DECISION, reason="decide"),
        AttentionItem(worker_id=worker.id, kind=AttentionKind.READY_TO_PUSH, reason="push"),
    ]
    snapshot = build(repo, workers=[worker], attention=items)
    assert snapshot.index("decide") < snapshot.index("push")
