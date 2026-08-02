"""Production native backend parity using tmux and a Claude-shaped hook fixture."""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from switchboard.agents.backend import WorkerEvent
from switchboard.agents.native_backend import NativeClaudeBackend, default_tmux_socket_path
from switchboard.agents.native_manager import MAX_HANDOFF_CHARS, PersistentNativeManager
from switchboard.config import ClaudeConfig, Config
from switchboard.core.session_manager import SessionManager, SessionManagerError
from switchboard.domain.enums import (
    ArtifactType,
    NativeTurnOrigin,
    RunStatus,
    RuntimeProcessState,
    WorkerRole,
    WorkerStatus,
)
from switchboard.runtime.hook_bridge import handle_hook
from switchboard.runtime.hook_bridge import main as hook_main
from switchboard.runtime.tmux import TmuxError
from switchboard.storage.store import Store

FAKE = Path(__file__).parents[1] / "fixtures" / "fake_native_claude.py"


async def wait_for(check, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = check()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("condition did not become true")


def test_long_state_directory_uses_a_short_stable_tmux_socket(tmp_path):
    state_dir = tmp_path / ("nested-" * 30)
    first = default_tmux_socket_path(state_dir)
    second = default_tmux_socket_path(state_dir)

    assert first == second
    assert len(str(first).encode()) <= 96
    assert first.parent in (Path("/private/tmp"), Path("/tmp"))


async def test_native_manager_is_persistent_adoptable_and_rotatable(native_services, tmp_path):
    sm, backend, _ = native_services
    manager = PersistentNativeManager(sm, backend, tmp_path / "manager-state")

    first = await manager.start_or_recover()
    reply = await manager.handle("What is blocked?")
    assert "Plan ready" in reply
    assert (await manager.start_or_recover()).id == first.id

    restarted = PersistentNativeManager(sm, backend, tmp_path / "manager-state")
    adopted = await restarted.start_or_recover()
    assert adopted.id == first.id
    assert adopted.claude_session_id == first.claude_session_id

    replacement = await restarted.rotate(
        {"objective": "continue active work", "rationale": "user changed priority"}
    )
    assert replacement.generation == first.generation + 1
    assert replacement.id != first.id
    handoff = sm.store.get_preference(f"manager.handoff.{replacement.id}", "") or ""
    assert 0 < len(handoff) <= MAX_HANDOFF_CHARS
    assert sm.store.get_preference("manager.handoff", "") == ""
    assert sm.store.get_runtime(first.id).owner.value == "human"


async def test_native_manager_human_entry_uses_same_process(native_services, tmp_path):
    sm, backend, _ = native_services
    manager = PersistentNativeManager(sm, backend, tmp_path / "manager-entry")
    runtime = await manager.start_or_recover()
    await manager.handle("Report status.")
    attachment = await manager.enter()
    assert attachment.session_id == runtime.claude_session_id
    assert "--resume" not in attachment.argv
    assert sm.store.get_runtime(runtime.id).owner.value == "human"
    manager.release_human(composer_cleared=True)
    assert sm.store.get_runtime(runtime.id).owner.value == "manager"


async def test_live_manager_adoption_mismatch_refuses_duplicate(
    native_services, tmp_path, monkeypatch
):
    sm, backend, _ = native_services
    manager = PersistentNativeManager(sm, backend, tmp_path / "manager-no-duplicate")
    runtime = await manager.start_or_recover()

    def mismatch(*args, **kwargs):
        raise TmuxError("fingerprint mismatch")

    monkeypatch.setattr(backend.runtime, "adopt", mismatch)
    with pytest.raises(TmuxError, match="Refusing to create a duplicate"):
        await manager.start_or_recover()
    assert sm.store.current_runtime(manager.manager_id).id == runtime.id
    assert len(sm.store.list_runtimes(manager.manager_id)) == 1


async def test_native_manager_entry_refuses_foreign_tmux_without_stranding_ownership(
    native_services, tmp_path, monkeypatch
):
    sm, backend, _ = native_services
    manager = PersistentNativeManager(sm, backend, tmp_path / "manager-nested")
    runtime = await manager.start_or_recover()
    monkeypatch.setenv("TMUX", "/tmp/a-different-tmux.sock,1,0")

    with pytest.raises(TmuxError, match="separate terminal"):
        await manager.enter()

    assert sm.store.get_runtime(runtime.id).owner.value == "manager"


@pytest.fixture
def native_services(store: Store, worktree_service, tmp_path: Path):
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    response = {
        "summary_lines": ["Native plan completed."],
        "decisions": [],
        "commit_stack": [],
        "risks": [],
        "base_commit": "",
        "criteria": [
            {
                "id": "AC1",
                "behavior": "Native Stop reaches artifact extraction.",
                "verification_method": "hook fixture",
                "evidence_required": ["Stop.last_assistant_message"],
            }
        ],
    }
    log = tmp_path / "native.jsonl"
    config = Config(
        claude=ClaudeConfig(
            executable=str(FAKE),
            env={
                "FAKE_NATIVE_LOG": str(log),
                "FAKE_NATIVE_COMPOSITE": "1",
                "FAKE_NATIVE_RESPONSE": "Plan ready.\n```json\n" + json.dumps(response) + "\n```",
            },
        )
    )
    socket = Path("/private/tmp") / f"switchboard-native-{uuid4().hex}.sock"
    backend = NativeClaudeBackend(store, config, tmp_path / "runtime", socket_path=socket)
    manager = SessionManager(store, backend, config, worktree_service)
    yield manager, backend, log
    subprocess.run(
        [backend.controller.executable, "-S", str(socket), "kill-server"],
        capture_output=True,
        check=False,
    )


async def test_atomic_workflow_consumes_only_managed_stop_and_returns_ready(
    native_services, git_repo
):
    manager, backend, log = native_services
    repo = manager.register_repository(git_repo("native-atomic"))
    job = manager.create_job("native plan", repo.id)

    worker = await manager.start_workflow("plan-feature", job_id=job.id, request="plan it")
    await wait_for(lambda: manager.store.get_worker(worker.id).status is WorkerStatus.IDLE)

    runtime = manager.store.current_runtime(worker.id)
    artifact = manager.store.latest_artifact(job.id, ArtifactType.IMPLEMENTATION_CONTRACT)
    behavior = manager.store.latest_artifact(job.id, ArtifactType.BEHAVIOR_CONTRACT)
    turns = manager.store.list_native_turns(runtime.id)
    assert runtime.process_state is RuntimeProcessState.READY
    assert artifact is not None
    assert behavior.body["criteria"][0]["id"] == "AC1"
    assert turns[-1].origin is NativeTurnOrigin.MANAGED
    assert turns[-1].claude_prompt_id
    assert worker.session_id == runtime.claude_session_id
    started = json.loads(
        (await wait_for(lambda: log.read_text() if log.exists() else "")).splitlines()[0]
    )
    assert started["argv"][started["argv"].index("--permission-mode") + 1] == "plan"
    transcript_count = len(manager.store.transcript(worker.id))
    artifact_count = len(manager.store.list_artifacts(job.id))
    stop_hook = next(
        event
        for event in manager.store.runtime_hook_events(runtime.id)
        if event.event_name == "Stop"
    )
    replay = backend._worker_event(worker.id, stop_hook)
    assert replay is not None
    manager._apply(replay)
    assert len(manager.store.transcript(worker.id)) == transcript_count
    assert len(manager.store.list_artifacts(job.id)) == artifact_count

    managed_artifact_id = artifact.id
    attachment = await manager.attach(worker.id)
    assert "--resume" not in attachment.argv
    assert "attach-session" in attachment.argv
    human_prompt_id = "human-prompt"
    handle_hook(
        manager.store,
        runtime.id,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": runtime.claude_session_id,
            "prompt_id": human_prompt_id,
            "prompt": "human output must not advance workflow",
        },
    )
    handle_hook(
        manager.store,
        runtime.id,
        {
            "hook_event_name": "Stop",
            "session_id": runtime.claude_session_id,
            "prompt_id": human_prompt_id,
            "last_assistant_message": '```json\n{"criteria": []}\n```',
        },
    )
    await asyncio.sleep(0.15)
    assert (
        manager.store.latest_artifact(job.id, ArtifactType.IMPLEMENTATION_CONTRACT).id
        == managed_artifact_id
    )
    manager.detach(worker.id, composer_cleared=True)
    assert manager.store.current_runtime(worker.id).process_state is RuntimeProcessState.READY


async def test_permission_and_busy_lane_are_normalized_without_prompt_corruption(
    native_services, git_repo
):
    manager, _, log = native_services
    repo = manager.register_repository(git_repo("native-lifecycle"))
    worker = await manager.create_worker(
        role=WorkerRole.GENERAL,
        title="native",
        prompt="PERMISSION_TEST HOLD_TURN",
        repository_id=repo.id,
    )
    await wait_for(
        lambda: (
            manager.store.current_runtime(worker.id).process_state is RuntimeProcessState.WAITING
        )
    )
    transcript_before = list(manager.store.transcript(worker.id))
    with pytest.raises(SessionManagerError, match="active turn|waiting"):
        await manager.send(worker.id, "SECOND_PROMPT_MUST_NOT_APPEAR")
    assert manager.store.transcript(worker.id) == transcript_before
    await wait_for(lambda: manager.store.get_worker(worker.id).status is WorkerStatus.IDLE)
    assert manager.store.current_runtime(worker.id).process_state is RuntimeProcessState.READY
    prompts = [
        json.loads(line)["text"]
        for line in log.read_text().splitlines()
        if '"event": "prompt"' in line
    ]
    assert len(prompts) == 1
    assert "SECOND_PROMPT_MUST_NOT_APPEAR" not in prompts[0]


async def test_only_a_starting_or_waiting_runtime_makes_a_send_retryable(
    native_services, git_repo
):
    """The backend, not the caller, decides that a refusal is worth retrying.

    Classifying every non-ready state as retryable would stop a genuinely dead session
    from ever being recorded as a disconnect.
    """
    from switchboard.agents.backend import WorkerNotReadyError

    manager, backend, _ = native_services
    repo = manager.register_repository(git_repo("native-not-ready"))
    worker = await manager.create_worker(
        role=WorkerRole.GENERAL, title="native", prompt="", repository_id=repo.id
    )
    runtime = manager.store.current_runtime(worker.id)

    for state in (RuntimeProcessState.STARTING, RuntimeProcessState.WAITING):
        runtime.process_state = state
        manager.store.save_runtime(runtime)
        with pytest.raises(WorkerNotReadyError):
            await backend.send(worker.id, "hello")

    for state in (RuntimeProcessState.EXITED, RuntimeProcessState.ABSENT):
        runtime.process_state = state
        manager.store.save_runtime(runtime)
        with pytest.raises(Exception) as caught:
            await backend.send(worker.id, "hello")
        assert not isinstance(caught.value, WorkerNotReadyError), state


async def test_human_entry_taints_active_managed_turn_and_notification_blocks(
    native_services, git_repo
):
    manager, backend, _ = native_services
    repo = manager.register_repository(git_repo("native-human-active"))
    worker = await manager.create_worker(
        role=WorkerRole.GENERAL,
        title="native",
        prompt="HOLD_TURN",
        repository_id=repo.id,
    )
    runtime = manager.store.current_runtime(worker.id)
    await wait_for(lambda: manager.store.open_native_turn(runtime.id))
    await manager.attach(worker.id)
    # Wait for the tainted turn to actually stop running rather than for a fixed delay,
    # which flakes under load: the point is that its output is discarded, not delayed.
    await wait_for(
        lambda: manager.store.current_runtime(worker.id).process_state
        is not RuntimeProcessState.TURN_ACTIVE
    )

    turn = manager.store.list_native_turns(runtime.id)[-1]
    assert turn.human_intervened
    assert manager.store.get_worker(worker.id).status is WorkerStatus.WORKING
    assert not [
        message for message in manager.store.transcript(worker.id) if message.role == "assistant"
    ]
    manager.detach(worker.id, composer_cleared=True)
    assert manager.store.current_runtime(worker.id).process_state is RuntimeProcessState.READY

    await manager.send(worker.id, "NOTIFICATION_PERMISSION HOLD_TURN")
    await wait_for(
        lambda: (
            manager.store.current_runtime(worker.id).process_state is RuntimeProcessState.WAITING
        )
    )
    await wait_for(lambda: manager.store.get_worker(worker.id).status is WorkerStatus.BLOCKED)


async def test_stop_failure_never_harvests_or_becomes_blocked(native_services, git_repo):
    manager, _, _ = native_services
    repo = manager.register_repository(git_repo("native-failure"))
    job = manager.create_job("failure", repo.id)
    worker = await manager.create_worker(
        role=WorkerRole.PLANNER,
        title="native",
        prompt="STOP_FAILURE",
        job_id=job.id,
    )
    await wait_for(lambda: manager.store.get_worker(worker.id).status is WorkerStatus.FAILED)

    assert manager.store.latest_artifact(job.id, ArtifactType.IMPLEMENTATION_CONTRACT) is None
    assert manager.store.current_runtime(worker.id).process_state is RuntimeProcessState.READY


async def test_native_composite_runs_end_to_end_through_real_tmux(native_services, git_repo):
    manager, _, _ = native_services
    repo = manager.register_repository(git_repo("native-composite"))
    job = manager.create_job("composite", repo.id)

    run = await manager.start_run("complete-ticket", job_id=job.id, request="native composite")
    paused = await wait_for(
        lambda: (
            candidate
            if (candidate := manager.store.get_run(run.id)).status is not RunStatus.RUNNING
            else None
        )
    )
    assert paused.status is RunStatus.AWAITING_APPROVAL, paused.detail
    manager.approve_plan(job.id)
    completed = await wait_for(
        lambda: (
            candidate
            if (candidate := manager.store.get_run(run.id)).status is RunStatus.COMPLETED
            else None
        ),
        timeout=15,
    )

    stored_job = manager.store.get_job(job.id)
    assert completed.iterations == {"0": 1, "1": 1, "2": 1, "3": 1, "7": 1}
    assert stored_job.authoritative_worktree_id is not None
    assert manager.store.latest_artifact(job.id, ArtifactType.VERIFICATION) is not None
    assert manager.store.latest_artifact(job.id, ArtifactType.REVIEW) is not None
    assert manager.job_completion(job.id).ready


async def test_recovery_advances_a_durably_completed_native_step_once(
    native_services, git_repo, monkeypatch
):
    manager, backend, _ = native_services
    repo = manager.register_repository(git_repo("native-composite-recovery"))
    job = manager.create_job("recover composite", repo.id)
    monkeypatch.setattr(manager, "_schedule_run_advance", lambda worker: None)

    run = await manager.start_run("complete-ticket", job_id=job.id)
    completed_step = await wait_for(
        lambda: (
            candidate
            if (candidate := manager.store.get_run(run.id)).current_step_completed
            else None
        )
    )
    worker_id = completed_step.current_worker_id
    manager._pumps.pop(worker_id).cancel()
    backend._sessions[worker_id].task.cancel()

    restarted_backend = NativeClaudeBackend(
        manager.store,
        manager.config,
        backend.runtime.state_dir.parent,
        socket_path=backend.controller.socket_path,
        tmux_executable=backend.controller.executable,
    )
    restarted = SessionManager(manager.store, restarted_backend, manager.config, manager.worktrees)
    notes = await restarted.recover()

    recovered = restarted.store.get_run(run.id)
    assert recovered.status is RunStatus.AWAITING_APPROVAL
    assert recovered.step_index == 0
    assert recovered.iterations == {"0": 1}
    assert any("composite run reconciled" in note for note in notes)


async def test_recovery_blocks_uncertain_prompt_delivery_until_human_reconciliation(
    native_services, git_repo, monkeypatch
):
    manager, backend, _ = native_services
    repo = manager.register_repository(git_repo("native-uncertain-delivery"))
    job = manager.create_job("uncertain delivery", repo.id)
    monkeypatch.setattr(backend.supervisor, "send", lambda runtime_id, text: None)

    run = await manager.start_run("complete-ticket", job_id=job.id)
    pending = manager.store.get_run(run.id)
    worker_id = pending.current_worker_id
    runtime = manager.store.current_runtime(worker_id)
    assert manager.store.list_native_turns(runtime.id)[-1].status.value == "pending"
    manager._pumps.pop(worker_id).cancel()
    backend._sessions[worker_id].task.cancel()

    restarted_backend = NativeClaudeBackend(
        manager.store,
        manager.config,
        backend.runtime.state_dir.parent,
        socket_path=backend.controller.socket_path,
        tmux_executable=backend.controller.executable,
    )
    restarted = SessionManager(manager.store, restarted_backend, manager.config, manager.worktrees)
    notes = await restarted.recover()

    blocked = restarted.store.get_run(run.id)
    assert blocked.status is RunStatus.BLOCKED
    assert "delivery is uncertain" in blocked.detail
    assert any("uncertain prompt delivery blocked" in note for note in notes)
    with pytest.raises(SessionManagerError, match="trusted completion"):
        await restarted.resume_run(run.id)

    await restarted.attach(worker_id)
    restarted.detach(worker_id, composer_cleared=True)
    await restarted.resume_run(run.id)
    await wait_for(lambda: restarted.store.get_run(run.id).status is RunStatus.AWAITING_APPROVAL)


async def test_human_intervention_taints_and_replays_the_same_native_step(
    native_services, git_repo
):
    manager, _, _ = native_services
    repo = manager.register_repository(git_repo("native-composite-human"))
    job = manager.create_job("human composite", repo.id)
    run = await manager.start_run("complete-ticket", job_id=job.id, request="HOLD_TURN")
    active = await wait_for(lambda: manager.store.get_run(run.id).current_worker_id)
    worker_id = active
    runtime = manager.store.current_runtime(worker_id)
    await wait_for(lambda: manager.store.open_native_turn(runtime.id))

    await manager.attach(worker_id)
    await asyncio.sleep(0.7)
    paused = manager.store.get_run(run.id)
    assert paused.status is RunStatus.BLOCKED
    assert not paused.current_step_completed
    assert manager.store.list_native_turns(runtime.id)[-1].human_intervened

    manager.detach(worker_id, composer_cleared=True)
    resumed = await manager.resume_run(run.id)
    assert resumed.iterations == {"0": 1}
    await wait_for(lambda: manager.store.get_run(run.id).status is RunStatus.AWAITING_APPROVAL)


async def test_failed_native_composite_turn_never_advances(native_services, git_repo):
    manager, _, _ = native_services
    repo = manager.register_repository(git_repo("native-composite-failure"))
    job = manager.create_job("failed composite", repo.id)

    run = await manager.start_run("complete-ticket", job_id=job.id, request="STOP_FAILURE")
    blocked = await wait_for(
        lambda: (
            candidate
            if (candidate := manager.store.get_run(run.id)).status is RunStatus.BLOCKED
            else None
        )
    )
    assert blocked.step_index == 0
    assert blocked.iterations == {"0": 1}
    assert not blocked.current_step_completed
    assert manager.store.latest_artifact(job.id, ArtifactType.IMPLEMENTATION_CONTRACT) is None


async def test_hook_application_and_delivery_marker_are_one_transaction(
    native_services, git_repo, monkeypatch
):
    manager, _, _ = native_services
    repo = manager.register_repository(git_repo("native-hook-transaction"))
    worker = await manager.create_worker(
        role=WorkerRole.GENERAL,
        title="native",
        prompt="",
        repository_id=repo.id,
    )
    hook_id = uuid4()
    event = WorkerEvent(
        worker_id=worker.id,
        type="text",
        text="must roll back with its delivery marker",
        data={"hook_event_id": str(hook_id)},
    )
    transcript_before = manager.store.transcript(worker.id)
    events_before = manager.store.recent_events(limit=100)

    def fail_marker(_event_id):
        raise RuntimeError("simulated marker failure")

    monkeypatch.setattr(manager.store, "mark_worker_hook_delivered", fail_marker)
    with pytest.raises(RuntimeError, match="marker failure"):
        manager._apply(event)

    assert manager.store.transcript(worker.id) == transcript_before
    assert manager.store.recent_events(limit=100) == events_before
    assert not manager.store.worker_hook_delivered(hook_id)


async def test_read_only_runtime_hook_durably_denies_native_write_tools(
    native_services, git_repo, monkeypatch
):
    manager, backend, _ = native_services
    repo = manager.register_repository(git_repo("native-read-only"))
    worker = await manager.create_worker(
        role=WorkerRole.REVIEWER,
        title="native",
        prompt="",
        repository_id=repo.id,
    )
    runtime = manager.store.current_runtime(worker.id)
    settings = json.loads(
        (backend.runtime.state_dir / f"native-{runtime.id}.settings.json").read_text()
    )
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "--deny-write-tools" in command

    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": runtime.claude_session_id,
                    "tool_name": "Write",
                }
            )
        ),
    )
    assert (
        hook_main(
            [
                "--database",
                str(manager.store.path),
                "--runtime-id",
                str(runtime.id),
                "--deny-write-tools",
            ]
        )
        == 2
    )
    assert manager.store.runtime_hook_events(runtime.id)[-1].event_name == "PreToolUse"

    # Policy fails closed even when observability cannot persist/correlate the event.
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": runtime.claude_session_id,
                    "tool_name": "Write",
                }
            )
        ),
    )
    assert (
        hook_main(
            [
                "--database",
                str(manager.store.path),
                "--runtime-id",
                str(uuid4()),
                "--deny-write-tools",
            ]
        )
        == 2
    )


async def test_restart_adopts_exact_native_process_and_continues_managed_turns(
    native_services, git_repo
):
    manager, backend, _ = native_services
    repo = manager.register_repository(git_repo("native-recovery"))
    worker = await manager.create_worker(
        role=WorkerRole.GENERAL,
        title="native",
        prompt="first",
        repository_id=repo.id,
    )
    await wait_for(lambda: manager.store.get_worker(worker.id).status is WorkerStatus.IDLE)
    before = manager.store.current_runtime(worker.id)
    before_pid = before.substrate["pane_pid"]
    before_session = before.claude_session_id
    before.process_state = RuntimeProcessState.TURN_COMPLETE
    manager.store.save_runtime(before)  # simulate crash after delivery but before acknowledgement
    manager._pumps.pop(worker.id).cancel()
    backend._sessions[worker.id].task.cancel()

    restarted_backend = NativeClaudeBackend(
        manager.store,
        manager.config,
        backend.runtime.state_dir.parent,
        socket_path=backend.controller.socket_path,
        tmux_executable=backend.controller.executable,
    )
    restarted = SessionManager(manager.store, restarted_backend, manager.config, manager.worktrees)
    notes = await restarted.recover()

    assert any("adopted" in note for note in notes)
    adopted = restarted.store.current_runtime(worker.id)
    assert adopted.substrate["pane_pid"] == before_pid
    assert adopted.claude_session_id == before_session
    await restarted.send(worker.id, "after restart")
    await wait_for(lambda: restarted.store.get_worker(worker.id).status is WorkerStatus.IDLE)
    assert len(restarted.store.list_native_turns(adopted.id)) == 2


async def test_missing_native_process_is_recreated_as_a_new_generation(native_services, git_repo):
    manager, backend, _ = native_services
    repo = manager.register_repository(git_repo("native-recreate"))
    worker = await manager.create_worker(
        role=WorkerRole.GENERAL,
        title="native",
        prompt="first",
        repository_id=repo.id,
    )
    await wait_for(lambda: manager.store.get_worker(worker.id).status is WorkerStatus.IDLE)
    first = manager.store.current_runtime(worker.id)
    first_session = first.claude_session_id
    manager._pumps.pop(worker.id).cancel()
    backend._sessions[worker.id].task.cancel()
    backend.supervisor.terminate(first.id)

    restarted_backend = NativeClaudeBackend(
        manager.store,
        manager.config,
        backend.runtime.state_dir.parent,
        socket_path=backend.controller.socket_path,
        tmux_executable=backend.controller.executable,
    )
    restarted = SessionManager(manager.store, restarted_backend, manager.config, manager.worktrees)
    notes = await restarted.recover()

    replacement = restarted.store.current_runtime(worker.id)
    assert any("recreated" in note for note in notes)
    assert replacement.generation == first.generation + 1
    assert replacement.id != first.id
    assert replacement.claude_session_id != first_session
    assert replacement.process_state is RuntimeProcessState.READY
