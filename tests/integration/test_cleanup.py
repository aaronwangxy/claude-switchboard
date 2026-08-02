"""Cleanup refuses to destroy work, and never deletes a branch."""

from __future__ import annotations

import asyncio

from switchboard.domain.enums import (
    WorkerRole,
    WorkerStatus,
)
from switchboard.gitops import runner
from tests.conftest import commit_file


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------- cleanup


async def test_cleanup_requires_confirmation_and_then_refuses_unsafe_removal(
    session_manager, git_repo
):
    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    job = sm.create_job("Cleanup", repo.id)
    worker = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER, title="w", prompt="hi", job_id=job.id, writable=True
    )
    await settle()
    worktree = sm.store.get_worktree(worker.worktree_id)

    unconfirmed = await sm.request_cleanup(worker_id=worker.id, confirmed=False)
    assert not unconfirmed.performed
    assert "confirmation" in unconfirmed.decision.explanation
    assert worktree.path.exists()

    (worktree.path / "scratch.txt").write_text("work in progress\n")
    refused = await sm.request_cleanup(worker_id=worker.id, confirmed=True)
    assert not refused.decision.safe
    assert "uncommitted" in refused.decision.explanation
    assert (worktree.path / "scratch.txt").exists(), "refusing must not lose work"


async def test_safe_cleanup_stops_the_worker_and_preserves_the_branch(session_manager, git_repo):
    sm = session_manager
    repo_path = git_repo("alpha")
    repo = sm.register_repository(repo_path, "alpha")
    job = sm.create_job("Cleanup", repo.id)
    worker = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER, title="w", prompt="hi", job_id=job.id, writable=True
    )
    await settle()
    worktree = sm.store.get_worktree(worker.worktree_id)

    result = await sm.request_cleanup(worker_id=worker.id, confirmed=True)
    assert result.performed and result.decision.safe
    assert not worktree.path.exists()
    assert str(worktree.path) not in runner.worktree_list(repo_path)
    assert runner.ref_exists(repo_path, worktree.branch), "cleanup never deletes branches"
    assert sm.store.get_worker(worker.id).status is WorkerStatus.STOPPED
    assert sm.store.get_worktree(worktree.id) is None


async def test_cleanup_refuses_to_discard_unmerged_commits(session_manager, git_repo):
    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    job = sm.create_job("Cleanup", repo.id)
    worker = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER, title="w", prompt="hi", job_id=job.id, writable=True
    )
    await settle()
    worktree = sm.store.get_worktree(worker.worktree_id)
    commit_file(worktree.path, "new.py", "X = 1\n", "feat: unmerged work")

    result = await sm.request_cleanup(worker_id=worker.id, confirmed=True)
    assert not result.decision.safe
    assert "not reachable from" in result.decision.explanation
    assert worktree.path.exists()
