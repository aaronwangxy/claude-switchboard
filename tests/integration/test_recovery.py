"""What a restarted controller does with the runtimes and runs it finds.

Adoption, reconstruction, refusal, and the durable Git baselines that decide what a
change invalidated while nobody was watching.
"""

from __future__ import annotations

import asyncio

import pytest

from switchboard.agents.backend import RuntimeObservation
from switchboard.agents.scripted_backend import ScriptedWorkerBackend
from switchboard.config import Config
from switchboard.core import lineage
from switchboard.core.session_manager import SessionManager
from switchboard.domain.enums import (
    ArtifactType,
    AttentionKind,
    RuntimeProcessState,
    WorkerRole,
    WorkerStatus,
)
from switchboard.domain.models import Artifact
from switchboard.gitops import runner
from tests.conftest import commit_file


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)


# --------------------------------------------------------------- recovery


async def test_restart_recreates_an_absent_runtime_from_its_stored_session(
    session_manager, git_repo, sb_home
):
    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    worker = await sm.create_worker(
        role=WorkerRole.GENERAL, title="w", prompt="hi", repository_id=repo.id
    )
    await settle()
    original_session = sm.store.get_worker(worker.id).session_id
    assert original_session

    fresh_backend = ScriptedWorkerBackend()
    restarted = SessionManager(sm.store, fresh_backend, Config(), sm.worktrees)
    notes = await restarted.recover()
    await settle()

    assert any("recreated" in note for note in notes)
    resumed_spec = next(s for s in fresh_backend.started if s.worker_id == worker.id)
    assert resumed_spec.resume_session_id == original_session
    runtimes = restarted.store.list_runtimes(worker.id)
    assert [runtime.generation for runtime in runtimes] == [1, 2]
    assert runtimes[0].process_state is RuntimeProcessState.ABSENT
    assert restarted.store.get_worker(worker.id).status is not WorkerStatus.DISCONNECTED


async def test_recovery_adopts_an_exact_live_runtime(session_manager, git_repo, backend):
    sm = session_manager
    repo = sm.register_repository(git_repo("adopt"), "adopt")
    worker = await sm.create_worker(
        role=WorkerRole.GENERAL, title="w", prompt="hi", repository_id=repo.id
    )
    await settle()
    sm.raise_attention(worker, AttentionKind.PERMISSION_REQUIRED, "Permission required for tool.")
    sm.raise_attention(worker, AttentionKind.PLAN_APPROVAL, "Approve the completed plan.")
    sm._pumps.pop(worker.id).cancel()

    restarted = SessionManager(sm.store, backend, Config(), sm.worktrees)
    notes = await restarted.recover()

    assert any("adopted" in note for note in notes)
    assert len(restarted.store.list_runtimes(worker.id)) == 1
    assert restarted.store.get_worker(worker.id).status is WorkerStatus.IDLE
    items = restarted.list_attention_items()
    assert len(items) == 1
    assert items[0].kind is AttentionKind.PLAN_APPROVAL


def test_status_summary_exposes_an_idle_incomplete_job(session_manager, git_repo):
    sm = session_manager
    repo = sm.register_repository(git_repo("idle-job"), "idle-job")
    sm.create_job("Greeting change", repo.id)

    summary = sm.status_summary()

    assert "1 incomplete job(s) are idle" in summary
    assert "Greeting change (intake)" in summary


async def test_recovery_preserves_the_observed_live_turn_state(
    session_manager, git_repo, backend
):
    sm = session_manager
    repo = sm.register_repository(git_repo("active-adopt"), "active-adopt")
    worker = await sm.create_worker(
        role=WorkerRole.GENERAL, title="w", prompt="hi", repository_id=repo.id
    )
    await settle()
    sm._pumps.pop(worker.id).cancel()
    runtime = sm.store.current_runtime(worker.id)

    async def active_observation(worker_id):
        return RuntimeObservation(
            exists=True,
            runtime_id=runtime.id,
            generation=runtime.generation,
            process_state=RuntimeProcessState.TURN_ACTIVE,
        )

    backend.observe = active_observation
    restarted = SessionManager(sm.store, backend, Config(), sm.worktrees)
    await restarted.recover()

    assert restarted.store.current_runtime(worker.id).process_state is RuntimeProcessState.TURN_ACTIVE
    assert restarted.store.get_worker(worker.id).status is WorkerStatus.WORKING


async def test_recovery_rejects_a_live_generation_mismatch(
    session_manager, git_repo, backend
):
    sm = session_manager
    repo = sm.register_repository(git_repo("stale"), "stale")
    worker = await sm.create_worker(
        role=WorkerRole.GENERAL, title="w", prompt="hi", repository_id=repo.id
    )
    await settle()
    sm._pumps.pop(worker.id).cancel()
    backend._sessions[worker.id].spec.runtime_generation += 1

    restarted = SessionManager(sm.store, backend, Config(), sm.worktrees)
    notes = await restarted.recover()

    assert any("stale runtime rejected" in note for note in notes)
    restored = restarted.store.get_worker(worker.id)
    assert restored.status is WorkerStatus.DISCONNECTED
    assert "Refusing to adopt" in restored.waiting_for


async def test_recovery_rejects_a_live_launch_fingerprint_mismatch(
    session_manager, git_repo, backend
):
    sm = session_manager
    repo = sm.register_repository(git_repo("config-drift"), "config-drift")
    worker = await sm.create_worker(
        role=WorkerRole.GENERAL, title="w", prompt="hi", repository_id=repo.id
    )
    await settle()
    sm._pumps.pop(worker.id).cancel()
    changed = Config(claude={"env": {"CONFIG_DRIFT": "yes"}})

    restarted = SessionManager(sm.store, backend, changed, sm.worktrees)
    notes = await restarted.recover()

    assert any("stale runtime rejected" in note for note in notes)
    assert restarted.store.get_worker(worker.id).status is WorkerStatus.DISCONNECTED


async def test_recovery_reconciles_a_durable_git_baseline(
    session_manager, git_repo
):
    sm = session_manager
    repo = sm.register_repository(git_repo("reconcile"), "reconcile")
    job = sm.create_job("Reconcile", repo.id)
    worker = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER,
        title="w",
        prompt="",
        job_id=job.id,
        writable=True,
    )
    head = runner.head_commit(worker.cwd)
    sm.store.save_artifact(
        Artifact(
            job_id=job.id,
            worker_id=worker.id,
            type=ArtifactType.REVIEW,
            head_commit=head,
            tree_hash=runner.tree_hash(worker.cwd),
            body={"verdict": "pass", "findings": []},
        )
    )
    lineage.snapshot_before_turn(sm.store, worker)
    commit_file(worker.cwd, "late.txt", "late\n", "late edit")

    restarted = SessionManager(sm.store, ScriptedWorkerBackend(), Config(), sm.worktrees)
    await restarted.recover()

    artifact = restarted.store.latest_artifact(job.id, ArtifactType.REVIEW)
    assert artifact.stale
    runtime = restarted.store.current_runtime(worker.id)
    assert runtime.git_head_before_turn is None
    assert runtime.git_tree_before_turn is None


async def test_a_new_turn_reconciles_an_unfinished_baseline_before_replacing_it(
    session_manager, git_repo
):
    sm = session_manager
    repo = sm.register_repository(git_repo("next-turn"), "next-turn")
    job = sm.create_job("Next turn", repo.id)
    worker = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER,
        title="w",
        prompt="",
        job_id=job.id,
        writable=True,
    )
    sm.store.save_artifact(
        Artifact(
            job_id=job.id,
            worker_id=worker.id,
            type=ArtifactType.VERIFICATION,
            head_commit=runner.head_commit(worker.cwd),
            tree_hash=runner.tree_hash(worker.cwd),
            body={"evidence": []},
        )
    )
    lineage.snapshot_before_turn(sm.store, worker)
    commit_file(worker.cwd, "between.txt", "changed\n", "between turns")

    await sm.send(worker.id, "continue")

    artifact = sm.store.latest_artifact(job.id, ArtifactType.VERIFICATION)
    assert artifact.stale
    runtime = sm.store.current_runtime(worker.id)
    assert runtime.git_head_before_turn == runner.head_commit(worker.cwd)


async def test_direct_workflow_start_reconciles_every_writable_worker_for_the_job(
    session_manager, git_repo
):
    sm = session_manager
    repo = sm.register_repository(git_repo("direct-workflow"), "direct-workflow")
    job = sm.create_job("Direct", repo.id)
    writer = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER,
        title="writer",
        prompt="",
        job_id=job.id,
        writable=True,
    )
    observer = await sm.create_worker(
        role=WorkerRole.QUESTION,
        title="observer",
        prompt="",
        job_id=job.id,
        writable=False,
    )
    sm.store.save_artifact(
        Artifact(
            job_id=job.id,
            worker_id=writer.id,
            type=ArtifactType.REVIEW,
            head_commit=runner.head_commit(writer.cwd),
            tree_hash=runner.tree_hash(writer.cwd),
            body={"verdict": "pass", "findings": []},
        )
    )
    lineage.snapshot_before_turn(sm.store, writer)
    commit_file(writer.cwd, "direct.txt", "changed\n", "direct change")

    await sm.start_workflow(
        "ask-question", job_id=job.id, target_worker_id=observer.id, request="inspect"
    )

    assert sm.store.latest_artifact(job.id, ArtifactType.REVIEW).stale


async def test_job_inspection_uses_the_explicit_authoritative_worktree(
    session_manager, git_repo
):
    sm = session_manager
    repo = sm.register_repository(git_repo("authoritative-lineage"), "lineage")
    job = sm.create_job("Lineage", repo.id)
    authoritative = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER,
        title="authoritative",
        prompt="",
        job_id=job.id,
        writable=True,
    )
    other = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER,
        title="isolated experiment",
        prompt="",
        job_id=job.id,
        writable=True,
    )
    commit_file(authoritative.cwd, "intended.txt", "yes\n", "intended lineage")
    commit_file(other.cwd, "wrong.txt", "no\n", "other lineage")

    stored = sm.store.get_job(job.id)
    assert stored.authoritative_worktree_id == authoritative.worktree_id
    assert lineage.inspection_path(sm.store, stored) == authoritative.cwd
    base, head, commits, diff = lineage.review_inputs(sm.store, stored)
    assert base and head
    assert "intended lineage" in commits
    assert "intended.txt" in diff
    assert "wrong.txt" not in diff

    sm.set_authoritative_worktree(job.id, other.worktree_id)
    switched = sm.store.get_job(job.id)
    assert lineage.inspection_path(sm.store, switched) == other.cwd
    _, _, switched_commits, switched_diff = lineage.review_inputs(sm.store, switched)
    assert "other lineage" in switched_commits
    assert "wrong.txt" in switched_diff
    assert "intended.txt" not in switched_diff


async def test_legacy_multiple_writer_job_requires_explicit_lineage(
    session_manager, git_repo
):
    sm = session_manager
    repo = sm.register_repository(git_repo("legacy-lineage"), "legacy")
    job = sm.create_job("Legacy", repo.id)
    first = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER, title="first", prompt="", job_id=job.id, writable=True
    )
    second = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER, title="second", prompt="", job_id=job.id, writable=True
    )
    legacy = sm.store.get_job(job.id)
    legacy.authoritative_worktree_id = None
    sm.store.save_job(legacy)

    with pytest.raises(Exception, match="multiple writable worktrees"):
        await sm.start_run("complete-ticket", job_id=job.id)

    sm.set_authoritative_worktree(job.id, second.worktree_id)
    observer = await sm.create_worker(
        role=WorkerRole.QUESTION,
        title="old observer",
        prompt="",
        job_id=job.id,
        writable=False,
    )
    sm.set_authoritative_worktree(job.id, first.worktree_id)
    with pytest.raises(Exception, match="observes a different worktree"):
        await sm.start_workflow(
            "ask-question", job_id=job.id, target_worker_id=observer.id, request="inspect"
        )


async def test_send_failure_does_not_leave_the_runtime_working(
    session_manager, git_repo, backend
):
    sm = session_manager
    repo = sm.register_repository(git_repo("send-failure"), "send-failure")
    worker = await sm.create_worker(
        role=WorkerRole.GENERAL, title="w", prompt="", repository_id=repo.id
    )

    async def fail_send(worker_id, message):
        raise RuntimeError("input channel closed")

    backend.send = fail_send
    with pytest.raises(Exception, match="input channel closed"):
        await sm.send(worker.id, "continue")

    assert sm.store.get_worker(worker.id).status is WorkerStatus.DISCONNECTED
    assert sm.store.current_runtime(worker.id).process_state is RuntimeProcessState.WAITING


async def test_a_worker_whose_worktree_vanished_is_marked_disconnected(
    session_manager, git_repo, sb_home
):
    import shutil

    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    job = sm.create_job("Recovery", repo.id)
    worker = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER, title="w", prompt="hi", job_id=job.id, writable=True
    )
    await settle()
    shutil.rmtree(sm.store.get_worktree(worker.worktree_id).path)

    restarted = SessionManager(sm.store, ScriptedWorkerBackend(), Config(), sm.worktrees)
    notes = await restarted.recover()

    restored = restarted.store.get_worker(worker.id)
    assert restored.status is WorkerStatus.DISCONNECTED
    assert "worktree is missing" in restored.waiting_for
    assert "replacement" in restored.waiting_for, "the explanation is actionable"
    assert any("worktree missing" in note for note in notes)


async def test_a_worker_with_no_session_id_is_reconstructed_from_durable_runtime(
    session_manager, git_repo
):
    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    worker = await sm.create_worker(
        role=WorkerRole.GENERAL, title="w", prompt="hi", repository_id=repo.id
    )
    await settle()
    worker = sm.store.get_worker(worker.id)
    worker.session_id = None
    sm.store.save_worker(worker)

    restarted = SessionManager(sm.store, ScriptedWorkerBackend(), Config(), sm.worktrees)
    await restarted.recover()
    assert restarted.store.get_worker(worker.id).status is WorkerStatus.IDLE
    assert restarted.store.get_worker(worker.id).session_id is not None
