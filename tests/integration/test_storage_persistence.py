"""The store is the system of record: everything must survive a process restart."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from switchboard.domain.enums import (
    ArtifactType,
    AttentionKind,
    JobStage,
    RuntimeOwner,
    RuntimeProcessState,
    WorkerRole,
    WorkerStatus,
)
from switchboard.domain.models import (
    Artifact,
    AttentionItem,
    Decision,
    Event,
    Job,
    Repository,
    RuntimeInstance,
    TranscriptMessage,
    Worker,
    WorkflowExecution,
    Worktree,
)
from switchboard.storage.store import Store

T0 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def reopen(sb_home: Path):
    """Close a store and hand back a brand new one on the same database file."""

    opened: list[Store] = []

    def _reopen(store: Store) -> Store:
        path = store.path
        store.close()
        fresh = Store(path)
        opened.append(fresh)
        return fresh

    yield _reopen
    for store in opened:
        store.close()


def test_every_entity_round_trips_across_a_reopen(store: Store, reopen, tmp_path: Path):
    repo = Repository(
        name="demo",
        root_path=tmp_path / "repos" / "demo",
        default_branch="trunk",
        registered_at=at(0),
    )
    job = Job(
        title="Add login",
        external_ref="ENG-1234",
        repository_id=repo.id,
        stage=JobStage.IMPLEMENTING,
        base_ref="trunk",
        ticket_text="Users need to log in.",
        created_at=at(1),
        updated_at=at(2),
    )
    worker = Worker(
        job_id=job.id,
        title="implement login",
        role=WorkerRole.IMPLEMENTER,
        status=WorkerStatus.WORKING,
        repository_id=repo.id,
        cwd=tmp_path / "worktrees" / "demo" / "eng-1234",
        session_id="sess-abc",
        model="some-model-id",
        writable=True,
        pinned=True,
        snoozed_until=at(60),
        workflow="plan-feature",
        created_at=at(3),
        updated_at=at(4),
    )
    worktree = Worktree(
        repository_id=repo.id,
        path=tmp_path / "worktrees" / "demo" / "eng-1234",
        branch="sb/eng-1234-abc",
        base_ref="trunk",
        owner_worker_id=worker.id,
        created_at=at(5),
    )
    worker.worktree_id = worktree.id
    event = Event(
        kind="worker.status_changed",
        job_id=job.id,
        worker_id=worker.id,
        summary="worker started working",
        payload={"from": "starting", "to": "working", "attempt": 1},
        created_at=at(6),
    )
    attention = AttentionItem(
        worker_id=worker.id,
        job_id=job.id,
        kind=AttentionKind.PLAN_APPROVAL,
        reason="plan needs approval",
        waiting_for="human",
        created_at=at(7),
    )
    message = TranscriptMessage(
        worker_id=worker.id, role="assistant", text="Here is the plan.", created_at=at(8)
    )
    decision = Decision(
        job_id=job.id, question="Which auth provider?", answer="OAuth", created_at=at(9)
    )
    artifact = Artifact(
        job_id=job.id,
        type=ArtifactType.IMPLEMENTATION_CONTRACT,
        worker_id=worker.id,
        base_commit="a" * 40,
        head_commit="b" * 40,
        tree_hash="c" * 40,
        stale=True,
        stale_reason="base moved",
        body={"steps": ["one", "two"], "risk": {"level": "low"}},
        created_at=at(10),
    )
    execution = WorkflowExecution(
        job_id=job.id,
        worker_id=worker.id,
        workflow="plan-feature",
        head_commit="d" * 40,
        status="completed",
        created_at=at(11),
    )

    store.add_repository(repo)
    store.save_job(job)
    store.save_worktree(worktree)
    store.save_worker(worker)
    store.add_event(event)
    store.save_attention_item(attention)
    store.add_transcript(message)
    store.add_decision(decision)
    store.save_artifact(artifact)
    store.add_workflow_execution(execution)
    store.set_preference("verbosity", "concise")

    reopened = reopen(store)

    loaded_repo = reopened.get_repository(repo.id)
    assert loaded_repo == repo
    assert isinstance(loaded_repo.id, type(repo.id)) and loaded_repo.id == repo.id
    assert isinstance(loaded_repo.root_path, Path)
    assert isinstance(loaded_repo.registered_at, datetime)
    assert loaded_repo.registered_at == at(0)
    assert loaded_repo.default_branch == "trunk"
    assert reopened.get_repository_by_path(repo.root_path) == repo
    assert reopened.get_repository_by_name("DEMO") == repo
    assert reopened.list_repositories() == [repo]

    loaded_job = reopened.get_job(job.id)
    assert loaded_job == job
    assert isinstance(loaded_job.stage, JobStage) and loaded_job.stage is JobStage.IMPLEMENTING
    assert loaded_job.repository_id == repo.id
    assert loaded_job.ticket_text == "Users need to log in."
    assert reopened.list_jobs(JobStage.IMPLEMENTING) == [job]
    assert reopened.active_jobs() == [job]

    loaded_worktree = reopened.get_worktree(worktree.id)
    assert loaded_worktree == worktree
    assert isinstance(loaded_worktree.path, Path)
    assert loaded_worktree.owner_worker_id == worker.id
    assert reopened.list_worktrees(repo.id) == [worktree]

    loaded_worker = reopened.get_worker(worker.id)
    assert loaded_worker == worker
    assert isinstance(loaded_worker.role, WorkerRole) and loaded_worker.role is WorkerRole.IMPLEMENTER
    assert isinstance(loaded_worker.status, WorkerStatus)
    assert isinstance(loaded_worker.cwd, Path)
    assert loaded_worker.worktree_id == worktree.id
    assert loaded_worker.writable is True and loaded_worker.pinned is True
    assert loaded_worker.snoozed_until == at(60)

    loaded_event = reopened.recent_events(limit=5)[0]
    assert loaded_event == event
    assert loaded_event.payload == {"from": "starting", "to": "working", "attempt": 1}
    assert reopened.recent_events(limit=5, job_id=job.id) == [event]

    loaded_attention = reopened.list_attention_items()[0]
    assert loaded_attention == attention
    assert isinstance(loaded_attention.kind, AttentionKind)
    assert reopened.attention_items_for_worker(worker.id) == [attention]

    assert reopened.transcript(worker.id) == [message]
    assert reopened.list_decisions(job.id) == [decision]

    loaded_artifact = reopened.get_artifact(artifact.id)
    assert loaded_artifact == artifact
    assert isinstance(loaded_artifact.type, ArtifactType)
    assert loaded_artifact.body == {"steps": ["one", "two"], "risk": {"level": "low"}}
    assert loaded_artifact.stale is True

    assert reopened.list_workflow_executions(job.id) == [execution]
    assert reopened.get_preference("verbosity") == "concise"


def test_list_workers_filters_by_job_and_status(store: Store, tmp_path: Path):
    repo = Repository(name="demo", root_path=tmp_path / "demo")
    store.add_repository(repo)
    job_a = Job(title="A", repository_id=repo.id)
    job_b = Job(title="B", repository_id=repo.id)
    store.save_job(job_a)
    store.save_job(job_b)

    def worker(job: Job | None, status: WorkerStatus, order: int) -> Worker:
        w = Worker(
            job_id=job.id if job else None,
            title=f"worker-{order}",
            repository_id=repo.id,
            cwd=tmp_path / "demo",
            status=status,
            created_at=at(order),
        )
        store.save_worker(w)
        return w

    a_working = worker(job_a, WorkerStatus.WORKING, 1)
    a_idle = worker(job_a, WorkerStatus.IDLE, 2)
    b_working = worker(job_b, WorkerStatus.WORKING, 3)
    unassigned = worker(None, WorkerStatus.STARTING, 4)

    assert [w.id for w in store.list_workers()] == [
        a_working.id,
        a_idle.id,
        b_working.id,
        unassigned.id,
    ]
    assert [w.id for w in store.list_workers(job_id=job_a.id)] == [a_working.id, a_idle.id]
    assert [w.id for w in store.list_workers(status=WorkerStatus.WORKING)] == [
        a_working.id,
        b_working.id,
    ]
    assert [w.id for w in store.list_workers(job_id=job_a.id, status=WorkerStatus.WORKING)] == [
        a_working.id
    ]
    assert store.list_workers(job_id=job_b.id, status=WorkerStatus.IDLE) == []


def test_latest_artifact_returns_the_most_recent_of_its_type(store: Store, tmp_path: Path):
    repo = Repository(name="demo", root_path=tmp_path / "demo")
    store.add_repository(repo)
    job = Job(title="A", repository_id=repo.id)
    store.save_job(job)

    older = Artifact(
        job_id=job.id,
        type=ArtifactType.REVIEW,
        body={"round": 1},
        created_at=at(1),
    )
    newer = Artifact(
        job_id=job.id,
        type=ArtifactType.REVIEW,
        body={"round": 2},
        created_at=at(2),
    )
    other_type = Artifact(
        job_id=job.id,
        type=ArtifactType.VERIFICATION,
        body={"passed": True},
        created_at=at(3),
    )
    for artifact in (newer, older, other_type):  # inserted out of order on purpose
        store.save_artifact(artifact)

    latest = store.latest_artifact(job.id, ArtifactType.REVIEW)
    assert latest is not None
    assert latest.id == newer.id
    assert latest.body == {"round": 2}
    assert store.latest_artifact(job.id, ArtifactType.VERIFICATION).id == other_type.id
    assert store.latest_artifact(job.id, ArtifactType.SMOKE_VERIFICATION) is None
    assert [a.id for a in store.list_artifacts(job.id, ArtifactType.REVIEW)] == [older.id, newer.id]


def test_list_attention_items_excludes_handled_by_default(store: Store, tmp_path: Path):
    repo = Repository(name="demo", root_path=tmp_path / "demo")
    store.add_repository(repo)
    worker = Worker(title="w", repository_id=repo.id, cwd=tmp_path / "demo")
    store.save_worker(worker)

    open_item = AttentionItem(
        worker_id=worker.id,
        kind=AttentionKind.PLAN_APPROVAL,
        reason="approve the plan",
        created_at=at(1),
    )
    handled_item = AttentionItem(
        worker_id=worker.id,
        kind=AttentionKind.READY_TO_PUSH,
        reason="already dealt with",
        handled=True,
        created_at=at(2),
    )
    store.save_attention_item(open_item)
    store.save_attention_item(handled_item)

    assert [i.id for i in store.list_attention_items()] == [open_item.id]
    assert [i.id for i in store.list_attention_items(include_handled=True)] == [
        open_item.id,
        handled_item.id,
    ]
    assert [i.id for i in store.attention_items_for_worker(worker.id)] == [open_item.id]

    # marking an item handled removes it from the default queue
    open_item.handled = True
    store.save_attention_item(open_item)
    assert store.list_attention_items() == []


def test_transcript_returns_messages_in_insertion_order(store: Store, tmp_path: Path):
    repo = Repository(name="demo", root_path=tmp_path / "demo")
    store.add_repository(repo)
    worker = Worker(title="w", repository_id=repo.id, cwd=tmp_path / "demo")
    other = Worker(title="other", repository_id=repo.id, cwd=tmp_path / "demo")
    store.save_worker(worker)
    store.save_worker(other)

    texts = ["hello", "thinking", "ran a tool", "done"]
    for index, text in enumerate(texts):
        store.add_transcript(
            TranscriptMessage(
                worker_id=worker.id, role="assistant", text=text, created_at=at(index)
            )
        )
    store.add_transcript(
        TranscriptMessage(worker_id=other.id, role="user", text="not mine", created_at=at(99))
    )

    assert [m.text for m in store.transcript(worker.id)] == texts
    assert [m.text for m in store.transcript(worker.id, limit=2)] == texts[:2]
    assert [m.text for m in store.transcript(other.id)] == ["not mine"]


def test_preferences_round_trip(store: Store, reopen):
    assert store.get_preference("missing") is None
    assert store.get_preference("missing", "fallback") == "fallback"

    store.set_preference("verbosity", "detailed")
    assert store.get_preference("verbosity") == "detailed"

    store.set_preference("verbosity", "concise")  # upsert
    assert store.get_preference("verbosity") == "concise"

    assert reopen(store).get_preference("verbosity") == "concise"


def test_runtime_generations_and_ownership_round_trip(store: Store, reopen):
    agent_id = uuid4()
    first = RuntimeInstance(
        agent_id=agent_id,
        generation=1,
        backend="scripted",
        claude_session_id="session-1",
        process_state=RuntimeProcessState.TURN_ACTIVE,
        launch_fingerprint="fingerprint",
        git_head_before_turn="abc",
        git_tree_before_turn="def",
    )
    second = RuntimeInstance(
        agent_id=agent_id,
        generation=2,
        backend="scripted",
        owner=RuntimeOwner.HUMAN,
        process_state=RuntimeProcessState.READY,
        substrate={"target": "opaque"},
    )
    store.save_runtime(first)
    store.save_runtime(second)

    fresh = reopen(store)
    assert fresh.get_runtime(first.id) == first
    assert fresh.current_runtime(agent_id) == second
    assert fresh.list_runtimes(agent_id) == [first, second]


def test_deleting_a_worker_removes_its_transcript_and_attention(store: Store, tmp_path: Path):
    repo = Repository(name="demo", root_path=tmp_path / "demo")
    store.add_repository(repo)
    worker = Worker(title="w", repository_id=repo.id, cwd=tmp_path / "demo")
    store.save_worker(worker)
    store.add_transcript(TranscriptMessage(worker_id=worker.id, role="user", text="hi"))
    store.save_attention_item(
        AttentionItem(worker_id=worker.id, kind=AttentionKind.WORKER_FAILED, reason="boom")
    )

    store.delete_worker(worker.id)

    assert store.get_worker(worker.id) is None
    assert store.transcript(worker.id) == []
    assert store.list_attention_items() == []


def test_unknown_ids_return_none(store: Store):
    assert store.get_repository(uuid4()) is None
    assert store.get_job(uuid4()) is None
    assert store.get_worker(uuid4()) is None
    assert store.get_worktree(uuid4()) is None
    assert store.get_artifact(uuid4()) is None
