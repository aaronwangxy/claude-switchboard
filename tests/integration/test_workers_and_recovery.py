"""Worker independence, concurrency, attention flow, recovery, and cleanup."""

from __future__ import annotations

import asyncio

import pytest

from switchboard.agents.backend import RuntimeObservation
from switchboard.agents.scripted_backend import ScriptedWorkerBackend
from switchboard.config import Config
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
from switchboard.routing.attention import next_actionable
from switchboard.storage.store import Store
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


async def test_live_native_startup_failure_requests_entry_and_prevents_duplicate(
    session_manager, git_repo, backend, monkeypatch
):
    repo = session_manager.register_repository(git_repo("startup-trust"))
    job = session_manager.create_job("Trust prompt", repo.id)

    async def timed_out(spec):
        # The real supervisor binds and persists the tmux target before it starts waiting
        # for SessionStart, so a trust prompt times out with the substrate already durable.
        runtime = session_manager.store.get_runtime(spec.runtime_id)
        runtime.substrate = {"kind": "tmux", "session_name": "switchboard-trust", "pane_id": "%3"}
        session_manager.store.save_runtime(runtime)
        raise RuntimeError("Timed out waiting for native Claude SessionStart.")

    async def still_alive(worker_id):
        return RuntimeObservation(exists=True, detail="native process is alive")

    monkeypatch.setattr(backend, "start", timed_out)
    monkeypatch.setattr(backend, "observe", still_alive)

    with pytest.raises(Exception, match="Could not start worker"):
        await session_manager.start_workflow("plan-feature", job_id=job.id)

    worker = session_manager.store.list_workers(job.id)[0]
    assert worker.status is WorkerStatus.BLOCKED
    assert "Ctrl+E to enter this session" in worker.waiting_for
    blocked_runtime = session_manager.store.current_runtime(worker.id)
    assert blocked_runtime.process_state is RuntimeProcessState.STARTING
    # Without the durable target, the Ctrl+E this very message asks for is refused.
    assert blocked_runtime.substrate.get("session_name") == "switchboard-trust"
    assert session_manager.list_attention_items()[0].kind is AttentionKind.PERMISSION_REQUIRED

    with pytest.raises(Exception, match="instead of starting a duplicate"):
        await session_manager.start_workflow("plan-feature", job_id=job.id)
    assert len(session_manager.store.list_workers(job.id)) == 1

    runtime = session_manager.store.current_runtime(worker.id)
    runtime.process_state = RuntimeProcessState.READY
    session_manager.store.save_runtime(runtime)
    delivered: list[str] = []

    async def deliver(worker_id, message):
        delivered.append(message)

    monkeypatch.setattr(backend, "send", deliver)
    assert await session_manager.resume_startup(worker.id)
    assert delivered and delivered[0].startswith("Plan this work")
    assert session_manager.store.get_worker(worker.id).status is WorkerStatus.WORKING
    assert not await session_manager.resume_startup(worker.id)


async def test_a_run_blocked_at_native_startup_recovers_once_the_prompt_is_cleared(
    session_manager, git_repo, backend, monkeypatch
):
    """The documented Ctrl+E recovery must actually put the composite back in flight.

    A worker created but blocked at Claude's trust prompt is still its step's worker. If
    the run does not own it, clearing the prompt leaves the run blocked forever and the
    board reports a worker that nothing is observing.
    """
    from switchboard.domain.enums import RunStatus

    sm = session_manager
    repo = sm.register_repository(git_repo("startup-run"), "startup-run")
    job = sm.create_job("Greeting", repo.id)
    real_start = backend.start

    async def timed_out(spec):
        await real_start(spec)  # the native session really is alive behind the prompt
        raise RuntimeError("Timed out waiting for native Claude SessionStart.")

    async def still_alive(worker_id):
        return RuntimeObservation(exists=True, detail="native process is alive")

    monkeypatch.setattr(backend, "start", timed_out)
    monkeypatch.setattr(backend, "observe", still_alive)

    run = await sm.start_run("lightweight-feature", job_id=job.id)
    (planner,) = sm.store.list_workers(job.id)
    run = sm.store.get_run(run.id)
    assert run.status is RunStatus.BLOCKED
    assert run.current_worker_id == planner.id, "the run must own the worker it created"
    assert sm._pumps.get(planner.id) is not None, "nothing would observe the unblocked turn"

    # The user enters the session, clears the trust prompt, and hands control back.
    runtime = sm.store.current_runtime(planner.id)
    runtime.process_state = RuntimeProcessState.READY
    sm.store.save_runtime(runtime)
    assert await sm.resume_startup(planner.id)

    assert sm.store.get_run(run.id).status is RunStatus.RUNNING
    assert sm.store.get_worker(planner.id).status is WorkerStatus.WORKING


async def test_handback_clears_a_permission_block_the_user_just_answered(
    session_manager, git_repo, backend
):
    """Entering a worker to answer its permission prompt must not leave it blocked.

    A worker left BLOCKED keeps a stale reason on the board and, being non-terminal,
    makes its own step unreplayable: the replay is refused as a duplicate workflow.
    """
    sm = session_manager
    repo = sm.register_repository(git_repo("permission-handback"), "permission-handback")
    job = sm.create_job("Greeting", repo.id)
    worker = await sm.start_workflow("plan-feature", job_id=job.id)
    sm._force_status(worker, WorkerStatus.BLOCKED, "Permission required for Bash.")
    sm.raise_attention(worker, AttentionKind.PERMISSION_REQUIRED, "Permission required for Bash.")

    await sm.attach(worker.id)
    runtime = sm.store.current_runtime(worker.id)
    runtime.process_state = RuntimeProcessState.READY  # the prompt was answered by hand
    sm.store.save_runtime(runtime)
    sm.detach(worker.id, composer_cleared=True)

    reconciled = sm.store.get_worker(worker.id)
    assert reconciled.status is WorkerStatus.IDLE
    assert not reconciled.waiting_for
    open_kinds = {i.kind for i in sm.store.attention_items_for_worker(worker.id) if not i.handled}
    assert AttentionKind.PERMISSION_REQUIRED not in open_kinds

    # A worker still waiting on its runtime keeps its block.
    still_waiting = await sm.start_workflow("plan-feature", job_id=job.id, target_worker_id=worker.id)
    sm._force_status(still_waiting, WorkerStatus.BLOCKED, "Permission required for Bash.")
    await sm.attach(still_waiting.id)
    runtime = sm.store.current_runtime(still_waiting.id)
    runtime.process_state = RuntimeProcessState.WAITING
    sm.store.save_runtime(runtime)
    sm.detach(still_waiting.id, composer_cleared=True)
    assert sm.store.get_worker(still_waiting.id).status is WorkerStatus.BLOCKED


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


async def test_transcript_survives_selection_and_restart(session_manager, git_repo, sb_home):
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
    reopened = Store(sb_home / "switchboard.db")
    restored = [m.text for m in reopened.transcript(worker.id)]
    assert restored == texts
    reopened.close()


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
    sm._snapshot_before_change(worker)
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
    sm._snapshot_before_change(worker)
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
    sm._snapshot_before_change(writer)
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
    assert sm._job_inspection_path(stored) == authoritative.cwd
    base, head, commits, diff = sm._review_inputs(stored)
    assert base and head
    assert "intended lineage" in commits
    assert "intended.txt" in diff
    assert "wrong.txt" not in diff

    sm.set_authoritative_worktree(job.id, other.worktree_id)
    switched = sm.store.get_job(job.id)
    assert sm._job_inspection_path(switched) == other.cwd
    _, _, switched_commits, switched_diff = sm._review_inputs(switched)
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
