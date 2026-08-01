"""Worker independence, concurrency, attention flow, recovery, and cleanup."""

from __future__ import annotations

import asyncio

import pytest

from csm.agents.scripted_backend import ScriptedWorkerBackend
from csm.config import Config
from csm.core.session_manager import SessionManager
from csm.domain.enums import AttentionKind, WorkerRole, WorkerStatus
from csm.gitops import runner
from csm.routing.attention import next_actionable
from csm.storage.store import Store
from tests.conftest import commit_file


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)


# ------------------------------------------------------------- independence


async def test_two_writable_workers_in_one_repo_get_separate_worktrees(
    session_manager, git_repo
):
    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    job = sm.create_job("Parallel work", repo.id)

    first = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER, title="first", prompt="go", job_id=job.id, writable=True
    )
    second = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER, title="second", prompt="go", job_id=job.id, writable=True
    )
    await settle()

    wt_a = sm.store.get_worktree(first.worktree_id)
    wt_b = sm.store.get_worktree(second.worktree_id)
    assert wt_a.path != wt_b.path
    assert wt_a.branch != wt_b.branch
    assert wt_a.owner_worker_id == first.id and wt_b.owner_worker_id == second.id

    listing = runner.worktree_list(repo.root_path)
    assert str(wt_a.path) in listing and str(wt_b.path) in listing

    # Neither worktree lives inside the user's source repository.
    assert repo.root_path not in wt_a.path.parents
    assert repo.root_path not in wt_b.path.parents


async def test_workers_in_different_repositories_are_independent(session_manager, git_repo):
    sm = session_manager
    alpha = sm.register_repository(git_repo("alpha"), "alpha")
    beta = sm.register_repository(git_repo("beta"), "beta")

    a = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER, title="a", prompt="go", repository_id=alpha.id, writable=True
    )
    b = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER, title="b", prompt="go", repository_id=beta.id, writable=True
    )
    await settle()

    assert a.session_id != b.session_id, "independent sessions"
    assert a.cwd != b.cwd, "independent working directories"
    assert sm.store.get_worktree(a.worktree_id).repository_id == alpha.id
    assert sm.store.get_worktree(b.worktree_id).repository_id == beta.id


async def test_review_and_question_workers_are_read_only_by_default(session_manager, git_repo):
    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    job = sm.create_job("Read-only roles", repo.id)

    for role in (WorkerRole.REVIEWER, WorkerRole.QUESTION, WorkerRole.PLANNER, WorkerRole.VERIFIER):
        worker = await sm.create_worker(
            role=role, title=role.value, prompt="look", job_id=job.id
        )
        await settle()
        assert worker.writable is False, role
        assert worker.worktree_id is None, role


async def test_a_read_only_workflow_is_refused_on_a_read_only_worker(session_manager, git_repo):
    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    job = sm.create_job("Guard", repo.id)
    reviewer = await sm.create_worker(
        role=WorkerRole.REVIEWER, title="reviewer", prompt="look", job_id=job.id
    )
    await settle()
    with pytest.raises(Exception, match="cannot run|read-only"):
        await sm.start_workflow("rebase-stack", job_id=job.id, target_worker_id=reviewer.id)


async def test_workers_never_receive_manager_tools(session_manager, git_repo, backend):
    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    worker = await sm.create_worker(
        role=WorkerRole.GENERAL, title="w", prompt="hi", repository_id=repo.id, writable=True
    )
    await settle()
    spec = next(s for s in backend.started if s.worker_id == worker.id)
    for tool in ("list_workers", "create_worker", "request_cleanup", "list_attention_items"):
        assert tool not in spec.system_prompt_append
    assert "registry" not in spec.system_prompt_append.lower()


# ------------------------------------------------------------- concurrency


async def test_one_worker_blocks_while_another_keeps_working(session_manager, git_repo, backend):
    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    job = sm.create_job("Concurrent", repo.id)

    slow = asyncio.Event()

    def blocking(spec, message):
        return "[NEEDS INPUT] Which compatibility strategy should I use?"

    backend.responses["planner"] = blocking
    blocked = await sm.create_worker(
        role=WorkerRole.PLANNER, title="Auth fix", prompt="plan it", job_id=job.id
    )
    working = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER, title="Cache bug", prompt="go", job_id=job.id, writable=True
    )
    await settle()

    assert sm.store.get_worker(blocked.id).status is WorkerStatus.BLOCKED
    assert sm.store.get_worker(working.id).status is not WorkerStatus.BLOCKED

    items = sm.list_attention_items()
    assert items[0].worker_id == blocked.id
    assert "compatibility strategy" in items[0].reason
    assert slow.is_set() is False  # nothing hung


async def test_answering_a_blocked_worker_resumes_it_and_advances_the_queue(
    session_manager, git_repo, backend
):
    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    job = sm.create_job("Queue", repo.id)

    backend.responses["planner"] = lambda spec, msg: "[NEEDS INPUT] Which strategy?"
    blocked = await sm.create_worker(
        role=WorkerRole.PLANNER, title="Auth fix", prompt="plan", job_id=job.id
    )
    ready = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER, title="Cache bug", prompt="go", job_id=job.id, writable=True
    )
    await settle()
    sm.raise_attention(ready, AttentionKind.READY_TO_PUSH, "Cache bug is ready to push.")
    sm.selected_worker_id = blocked.id

    # The user answers; the worker returns to working and its item is handled.
    backend.responses["planner"] = lambda spec, msg: "Thanks, proceeding."
    await sm.send(blocked.id, "Use the read-legacy strategy.")
    await settle()

    assert sm.store.get_worker(blocked.id).status is not WorkerStatus.BLOCKED
    remaining = sm.list_attention_items()
    assert [i.worker_id for i in remaining] == [ready.id]

    workers = {w.id: w for w in sm.store.list_workers()}
    assert (
        next_actionable(
            remaining,
            workers,
            current_worker_id=blocked.id,
            auto_advance=sm.auto_advance,
            user_is_typing=False,
        )
        == ready.id
    )
    # ...but not while the user is mid-message.
    assert (
        next_actionable(
            remaining,
            workers,
            current_worker_id=blocked.id,
            auto_advance=sm.auto_advance,
            user_is_typing=True,
        )
        is None
    )


async def test_interrupting_a_blocked_worker_clears_its_attention(
    session_manager, git_repo, backend
):
    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    job = sm.create_job("Interrupt", repo.id)
    backend.responses["planner"] = lambda spec, msg: "[NEEDS INPUT] Which strategy?"
    worker = await sm.create_worker(
        role=WorkerRole.PLANNER, title="planner", prompt="plan", job_id=job.id
    )
    await settle()
    assert sm.list_attention_items()

    await sm.interrupt_worker(worker.id)
    await settle()

    assert sm.store.get_worker(worker.id).status is WorkerStatus.IDLE
    assert sm.list_attention_items() == [], "an interrupted worker stops asking for the user"
    assert any(
        m.text == "[interrupted by the user]" for m in sm.store.transcript(worker.id)
    ), "the interruption is visible in the transcript"


async def test_pin_and_snooze_are_persisted(session_manager, git_repo):
    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    worker = await sm.create_worker(
        role=WorkerRole.GENERAL, title="w", prompt="hi", repository_id=repo.id, writable=True
    )
    await settle()
    sm.toggle_pin(worker.id)
    sm.snooze(worker.id, minutes=45)
    stored = sm.store.get_worker(worker.id)
    assert stored.pinned is True
    assert stored.snoozed_until is not None


# ------------------------------------------------------------- transcripts


async def test_transcript_survives_selection_and_restart(session_manager, git_repo, csm_home):
    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    worker = await sm.create_worker(
        role=WorkerRole.GENERAL, title="w", prompt="first question", repository_id=repo.id
    )
    await settle()
    await sm.send(worker.id, "follow-up question")
    await settle()

    texts = [m.text for m in sm.store.transcript(worker.id)]
    assert "first question" in texts and "follow-up question" in texts

    sm.store.close()
    reopened = Store(csm_home / "csm.db")
    restored = [m.text for m in reopened.transcript(worker.id)]
    assert restored == texts
    reopened.close()


# --------------------------------------------------------------- recovery


async def test_restart_resumes_a_session_by_its_stored_id(session_manager, git_repo, csm_home):
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

    assert any("resumed" in note for note in notes)
    resumed_spec = next(s for s in fresh_backend.started if s.worker_id == worker.id)
    assert resumed_spec.resume_session_id == original_session
    assert restarted.store.get_worker(worker.id).status is not WorkerStatus.DISCONNECTED


async def test_a_worker_whose_worktree_vanished_is_marked_disconnected(
    session_manager, git_repo, csm_home
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


async def test_a_worker_with_no_session_id_is_never_reported_as_running(
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
    assert restarted.store.get_worker(worker.id).status is WorkerStatus.DISCONNECTED


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
