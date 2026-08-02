"""Answering a native workspace-trust prompt is consent recorded once, not a bypass.

Claude stores workspace trust per exact directory and every writable worker gets a fresh
worktree path, so without this each new worker stops on the same question about a
directory Switchboard created itself. The user still answers it -- once per repository --
and every guard around that answer is enforced in Python.
"""

from __future__ import annotations

import pytest

from switchboard.core.errors import SessionManagerError
from switchboard.domain.enums import RuntimeProcessState, WorkerRole


@pytest.fixture
async def worker(session_manager, git_repo):
    repo = session_manager.register_repository(git_repo("alpha"), "alpha")
    job = session_manager.create_job("ENG-1", repo.id)
    created = await session_manager.create_worker(
        role=WorkerRole.IMPLEMENTER, title="impl", prompt="", job_id=job.id, writable=True
    )
    return session_manager, repo, created


async def test_trust_is_not_granted_without_explicit_confirmation(worker):
    sm, repo, _ = worker
    with pytest.raises(SessionManagerError, match="explicit confirmation"):
        sm.grant_repository_trust(repo.id, confirmed=False)
    assert not sm.repository_trust_granted(repo.id)


async def test_an_untrusted_repository_refuses_to_answer_for_the_user(worker):
    sm, _, created = worker
    with pytest.raises(SessionManagerError, match="not trusted yet"):
        await sm.answer_workspace_trust(created.id)


async def test_a_directory_switchboard_does_not_own_is_never_answered_for(
    worker, tmp_path, monkeypatch
):
    """The guard is the path, not the model's say-so."""
    sm, repo, created = worker
    sm.grant_repository_trust(repo.id, confirmed=True)
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    created.cwd = elsewhere
    sm.store.save_worker(created)

    with pytest.raises(SessionManagerError, match="neither this repository nor a worktree"):
        await sm.answer_workspace_trust(created.id, confirmed=True)


async def test_a_session_that_is_not_asking_about_trust_is_left_alone(worker, monkeypatch):
    """A pane showing something else must never be sent a keystroke."""
    sm, repo, created = worker
    sm.grant_repository_trust(repo.id, confirmed=True)
    runtime = sm.store.current_runtime(created.id)
    runtime.process_state = RuntimeProcessState.STARTING
    sm.store.save_runtime(runtime)
    monkeypatch.setattr(sm.backend, "capture", lambda _id: "Do you want to allow rm -rf?")

    with pytest.raises(SessionManagerError, match="not showing a workspace-trust prompt"):
        await sm.answer_workspace_trust(created.id, confirmed=True)


async def test_a_ready_worker_is_not_treated_as_waiting_on_startup(worker):
    sm, repo, created = worker
    sm.grant_repository_trust(repo.id, confirmed=True)
    runtime = sm.store.current_runtime(created.id)
    runtime.process_state = RuntimeProcessState.READY
    sm.store.save_runtime(runtime)

    with pytest.raises(SessionManagerError, match="not waiting on a startup prompt"):
        await sm.answer_workspace_trust(created.id, confirmed=True)


async def test_consent_recorded_once_answers_the_prompt_for_a_later_worktree(
    worker, monkeypatch
):
    sm, repo, created = worker
    sm.grant_repository_trust(repo.id, confirmed=True)
    runtime = sm.store.current_runtime(created.id)
    runtime.process_state = RuntimeProcessState.STARTING
    sm.store.save_runtime(runtime)
    monkeypatch.setattr(
        sm.backend, "capture", lambda _id: "Quick safety check: I trust this folder"
    )
    answered: list[object] = []

    async def record(worker_id):
        answered.append(worker_id)

    monkeypatch.setattr(sm.backend, "answer_startup_dialog", record)

    assert await sm.answer_workspace_trust(created.id) is True
    assert answered == [created.id]


def test_vouching_reads_as_consent_in_an_ordinary_sentence():
    """A bare "confirm" message would make the user answer twice; the guard stays honest."""
    from switchboard.agents.manager import TRUST_RE

    for granted in (
        "Register that path as taskq. Yes, I confirm: trust the Switchboard worktrees.",
        "I trust the worktrees for taskq.",
        "yes, trust it",
        "confirm",
    ):
        assert TRUST_RE.search(granted), granted

    for withheld in (
        "Do not trust that repository.",
        "Should I trust it?",
        'The worker said "trust it".',
        "Trust it after Sam has looked at the repo.",
        "Never trust anything under /tmp.",
    ):
        assert not TRUST_RE.search(withheld), withheld


async def test_a_slow_start_is_not_mistaken_for_one_needing_a_person(worker, monkeypatch):
    """Several native sessions starting at once contend; slow is not stuck.

    Found by dogfooding: the sixth Claude in a fleet exceeded the startup wait, was
    declared blocked, and stalled a workflow run that nothing was actually wrong with.
    """
    sm, repo, created = worker
    monkeypatch.setattr(sm.backend, "capture", lambda _id: "❯ \n")
    waits: list[float] = []

    async def slow_ready(worker_id, timeout=30.0):
        waits.append(timeout)
        return True

    monkeypatch.setattr(sm.backend, "wait_ready", slow_ready)

    assert await sm._recover_startup(created) is True
    assert waits == [sm.SLOW_STARTUP_GRACE], "it waits again rather than asking the user"


async def test_a_slow_start_still_gives_up_eventually(worker, monkeypatch):
    sm, repo, created = worker
    monkeypatch.setattr(sm.backend, "capture", lambda _id: "❯ \n")

    async def never_ready(worker_id, timeout=30.0):
        return False

    monkeypatch.setattr(sm.backend, "wait_ready", never_ready)
    assert await sm._recover_startup(created) is False


async def test_a_trust_dialog_is_still_answered_rather_than_waited_out(worker, monkeypatch):
    sm, repo, created = worker
    sm.grant_repository_trust(repo.id, confirmed=True)
    runtime = sm.store.current_runtime(created.id)
    runtime.process_state = RuntimeProcessState.STARTING
    sm.store.save_runtime(runtime)
    monkeypatch.setattr(sm.backend, "capture", lambda _id: "Quick safety check: I trust this folder")
    answered: list[object] = []

    async def record(worker_id):
        answered.append(worker_id)

    async def ready(worker_id, timeout=30.0):
        return True

    monkeypatch.setattr(sm.backend, "answer_startup_dialog", record)
    monkeypatch.setattr(sm.backend, "wait_ready", ready)

    assert await sm._recover_startup(created) is True
    assert answered == [created.id]
