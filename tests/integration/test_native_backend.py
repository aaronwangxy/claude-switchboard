"""Production native backend parity using tmux and a Claude-shaped hook fixture."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from switchboard.agents.native_backend import NativeClaudeBackend
from switchboard.config import ClaudeConfig, Config
from switchboard.core.session_manager import SessionManager, SessionManagerError
from switchboard.domain.enums import (
    ArtifactType,
    NativeTurnOrigin,
    RuntimeProcessState,
    WorkerRole,
    WorkerStatus,
)
from switchboard.runtime.hook_bridge import handle_hook
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
                "FAKE_NATIVE_RESPONSE": "Plan ready.\n```json\n"
                + json.dumps(response)
                + "\n```",
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
    manager, backend, _ = native_services
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
    assert manager.store.latest_artifact(job.id, ArtifactType.IMPLEMENTATION_CONTRACT).id == managed_artifact_id
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
        lambda: manager.store.current_runtime(worker.id).process_state
        is RuntimeProcessState.WAITING
    )
    transcript_before = list(manager.store.transcript(worker.id))
    with pytest.raises(SessionManagerError, match="active turn|waiting"):
        await manager.send(worker.id, "SECOND_PROMPT_MUST_NOT_APPEAR")
    assert manager.store.transcript(worker.id) == transcript_before
    await wait_for(lambda: manager.store.get_worker(worker.id).status is WorkerStatus.IDLE)
    assert manager.store.current_runtime(worker.id).process_state is RuntimeProcessState.READY
    prompts = [json.loads(line)["text"] for line in log.read_text().splitlines() if '"event": "prompt"' in line]
    assert len(prompts) == 1
    assert "SECOND_PROMPT_MUST_NOT_APPEAR" not in prompts[0]


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
    manager._pumps.pop(worker.id).cancel()
    backend._sessions[worker.id].task.cancel()

    restarted_backend = NativeClaudeBackend(
        manager.store,
        manager.config,
        backend.runtime.state_dir.parent,
        socket_path=backend.controller.socket_path,
        tmux_executable=backend.controller.executable,
    )
    restarted = SessionManager(
        manager.store, restarted_backend, manager.config, manager.worktrees
    )
    notes = await restarted.recover()

    assert any("adopted" in note for note in notes)
    adopted = restarted.store.current_runtime(worker.id)
    assert adopted.substrate["pane_pid"] == before_pid
    assert adopted.claude_session_id == before_session
    await restarted.send(worker.id, "after restart")
    await wait_for(lambda: restarted.store.get_worker(worker.id).status is WorkerStatus.IDLE)
    assert len(restarted.store.list_native_turns(adopted.id)) == 2


async def test_missing_native_process_is_recreated_as_a_new_generation(
    native_services, git_repo
):
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
    restarted = SessionManager(
        manager.store, restarted_backend, manager.config, manager.worktrees
    )
    notes = await restarted.recover()

    replacement = restarted.store.current_runtime(worker.id)
    assert any("recreated" in note for note in notes)
    assert replacement.generation == first.generation + 1
    assert replacement.id != first.id
    assert replacement.claude_session_id != first_session
    assert replacement.process_state is RuntimeProcessState.READY
