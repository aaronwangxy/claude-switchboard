"""The native Claude prototype stays outside production worker routing."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from switchboard.config import ClaudeConfig, Config
from switchboard.domain.enums import (
    NativeTurnOrigin,
    NativeTurnStatus,
    RuntimeAgentKind,
    RuntimeProcessState,
)
from switchboard.domain.models import NativeTurn, RuntimeInstance
from switchboard.runtime.hook_bridge import handle_hook
from switchboard.runtime.native_claude import HOOK_EVENTS, NativeClaudePrototype
from switchboard.runtime.tmux import TmuxError


class RecordingSupervisor:
    def __init__(self) -> None:
        self.launches: list[tuple] = []
        self.sent: list[tuple] = []
        self.interrupted: list = []
        self.launch_result = object()

    def launch(self, runtime_id, argv, *, cwd, env=None):
        self.launches.append((runtime_id, tuple(argv), cwd, env))
        return self.launch_result

    def send(self, runtime_id, prompt):
        self.sent.append((runtime_id, prompt))

    def observe(self, runtime_id):
        return ("observed", runtime_id)

    def interrupt(self, runtime_id):
        self.interrupted.append(runtime_id)


def runtime(store, *, fingerprint="native-test"):
    return store.save_runtime(
        RuntimeInstance(
            agent_id=uuid4(),
            agent_kind=RuntimeAgentKind.WORKER,
            backend="native-prototype",
            launch_fingerprint=fingerprint,
            process_state=RuntimeProcessState.READY,
        )
    )


def test_launch_respects_executable_settings_sources_environment_and_hook_overlay(
    store, tmp_path: Path
):
    wrapper = tmp_path / "company-claude"
    wrapper.write_text("#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o755)
    supervisor = RecordingSupervisor()
    prototype = NativeClaudePrototype(
        store,
        supervisor,  # type: ignore[arg-type]
        Config(
            claude=ClaudeConfig(executable=str(wrapper), env={"COMPANY_PROXY": "configured"}),
            setting_sources=["user", "project", "local"],
        ),
        tmp_path / "state",
    )
    fingerprint = prototype.launch_fingerprint(cwd=tmp_path)
    instance = runtime(store, fingerprint=fingerprint)

    launch = prototype.launch(instance.id, cwd=tmp_path)

    assert launch.executable == wrapper
    assert store.get_runtime(instance.id).claude_session_id == launch.expected_session_id
    assert launch.argv[0] == str(wrapper)
    assert launch.argv[-2:] == ("--setting-sources", "user,project,local")
    assert supervisor.launches[0][2:] == (tmp_path, {"COMPANY_PROXY": "configured"})
    overlay = json.loads(launch.settings_overlay.read_text())
    assert set(overlay["hooks"]) == set(HOOK_EVENTS)
    command = overlay["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "switchboard.runtime.hook_bridge" in command
    assert str(instance.id) in command
    assert launch.settings_overlay.stat().st_mode & 0o777 == 0o600

    recovered = prototype.launch(instance.id, cwd=tmp_path)
    assert recovered.expected_session_id == launch.expected_session_id


def test_launch_rejects_a_runtime_bound_to_different_configuration(store, tmp_path: Path):
    wrapper = tmp_path / "claude"
    wrapper.write_text("#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o755)
    prototype = NativeClaudePrototype(
        store,
        RecordingSupervisor(),  # type: ignore[arg-type]
        Config(claude=ClaudeConfig(executable=str(wrapper))),
        tmp_path / "state",
    )
    instance = runtime(store, fingerprint="sha256:stale")

    with pytest.raises(TmuxError, match="fingerprint"):
        prototype.launch(instance.id, cwd=tmp_path)


def test_pending_turn_owns_the_input_lane_before_user_prompt_submit(store, tmp_path: Path):
    supervisor = RecordingSupervisor()
    prototype = NativeClaudePrototype(
        store, supervisor, Config(), tmp_path / "state"  # type: ignore[arg-type]
    )
    instance = runtime(store)

    first = prototype.send_managed(instance.id, "first")

    assert first.status is NativeTurnStatus.PENDING
    assert len(supervisor.sent) == 1
    with pytest.raises(TmuxError, match="active turn"):
        prototype.send_managed(instance.id, "must not be injected")

    handle_hook(
        store,
        instance.id,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session",
            "prompt_id": "prompt",
            "prompt": supervisor.sent[0][1],
        },
    )
    assert store.get_native_turn(first.id).status is NativeTurnStatus.ACTIVE


def test_failed_input_injection_closes_pending_turn(store, tmp_path: Path):
    class FailingSupervisor(RecordingSupervisor):
        def send(self, runtime_id, prompt):
            raise TmuxError("pane disappeared")

    prototype = NativeClaudePrototype(
        store, FailingSupervisor(), Config(), tmp_path / "state"  # type: ignore[arg-type]
    )
    instance = runtime(store)

    with pytest.raises(TmuxError, match="pane disappeared"):
        prototype.send_managed(instance.id, "hello")

    turn = store.list_native_turns(instance.id)[0]
    assert turn.status is NativeTurnStatus.FAILED
    assert turn.error == "Input injection failed: pane disappeared"


def test_database_enforces_one_open_native_turn_per_runtime(store):
    instance = runtime(store)
    store.save_native_turn(NativeTurn(runtime_id=instance.id, origin=NativeTurnOrigin.MANAGED))

    with pytest.raises(sqlite3.IntegrityError):
        store.save_native_turn(
            NativeTurn(runtime_id=instance.id, origin=NativeTurnOrigin.MANAGED)
        )


def test_adoption_validates_hook_configuration_and_interrupt_only_records_request(
    store, tmp_path: Path
):
    supervisor = RecordingSupervisor()
    prototype = NativeClaudePrototype(
        store, supervisor, Config(), tmp_path / "state"  # type: ignore[arg-type]
    )
    fingerprint = prototype.launch_fingerprint(cwd=tmp_path)
    instance = runtime(store, fingerprint=fingerprint)

    assert prototype.adopt(instance.id, cwd=tmp_path) == ("observed", instance.id)
    incompatible = NativeClaudePrototype(
        store,
        supervisor,  # type: ignore[arg-type]
        Config(),
        tmp_path / "different-state",
    )
    with pytest.raises(TmuxError, match="fingerprint"):
        incompatible.adopt(instance.id, cwd=tmp_path)

    turn = store.save_native_turn(
        NativeTurn(
            runtime_id=instance.id,
            origin=NativeTurnOrigin.MANAGED,
            status=NativeTurnStatus.ACTIVE,
        )
    )
    interrupted = prototype.interrupt(instance.id, turn.id)

    assert supervisor.interrupted == [instance.id]
    assert interrupted.status is NativeTurnStatus.INTERRUPT_REQUESTED
    assert prototype.completed(turn.id) is None
    assert store.get_runtime(instance.id).process_state is RuntimeProcessState.TURN_ACTIVE
