"""What Switchboard hands to native Claude rather than reimplementing.

Permission mode, effort and the session's display name are Claude's own features. Getting
them from configuration and passing them through is the whole implementation -- there is no
Switchboard permission engine, no Switchboard effort model, and no Switchboard session
registry. The one judgement here is the default: a writable worker owns an isolated
worktree on its own branch, so it runs with `acceptEdits` and does not stop on every write.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from switchboard.config import Config
from switchboard.runtime.native_claude import NativeClaudeRuntime


class _Recorder:
    """A supervisor stand-in that captures the argv the runtime would launch."""

    def __init__(self) -> None:
        self.argv: tuple[str, ...] = ()

    def launch(self, runtime_id, argv, *, cwd, env):
        self.argv = tuple(argv)
        raise _Launched


class _Launched(Exception):
    pass


@pytest.fixture
def runtime(store, tmp_path: Path):
    from switchboard.domain.models import RuntimeInstance

    supervisor = _Recorder()
    native = NativeClaudeRuntime(store, supervisor, Config(), tmp_path / "hooks")
    native._executable = lambda: Path("/bin/echo")  # type: ignore[method-assign]
    instance = RuntimeInstance(agent_id=uuid4(), backend="native-claude")
    store.save_runtime(instance)
    return native, supervisor, instance


def _launch(runtime, tmp_path: Path, **kwargs) -> tuple[str, ...]:
    native, supervisor, instance = runtime
    instance.launch_fingerprint = native.launch_fingerprint(cwd=tmp_path, **kwargs)
    native.store.save_runtime(instance)
    with pytest.raises(_Launched):
        native.launch(instance.id, cwd=tmp_path, **kwargs)
    return supervisor.argv


def test_permission_mode_effort_and_name_reach_the_cli(runtime, tmp_path):
    argv = _launch(
        runtime, tmp_path, permission_mode="acceptEdits", effort="high", session_name="ENG-1 fix"
    )
    assert "--permission-mode" in argv and argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--effort" in argv and argv[argv.index("--effort") + 1] == "high"
    assert "--name" in argv and argv[argv.index("--name") + 1] == "ENG-1 fix"


def test_nothing_is_passed_when_nothing_is_configured(runtime, tmp_path):
    argv = _launch(runtime, tmp_path)
    assert "--permission-mode" not in argv
    assert "--effort" not in argv
    assert "--name" not in argv


def test_the_fingerprint_covers_the_delegated_flags(runtime, tmp_path):
    """Otherwise a live session launched under different flags would be adopted as a match."""
    native, _, _ = runtime
    base = native.launch_fingerprint(cwd=tmp_path)
    assert native.launch_fingerprint(cwd=tmp_path, permission_mode="acceptEdits") != base
    assert native.launch_fingerprint(cwd=tmp_path, effort="high") != base
    assert native.launch_fingerprint(cwd=tmp_path, session_name="a") != base


def test_a_writable_worker_does_not_stop_on_every_file_write():
    config = Config()
    assert config.permission_mode_for(writable=True) == "acceptEdits"
    assert config.permission_mode_for(writable=False) == "plan"


def test_permission_mode_is_configurable_including_off():
    config = Config.model_validate({"permissions": {"writable_worker": None}})
    assert config.permission_mode_for(writable=True) is None


def test_a_workflow_may_name_its_own_permission_mode():
    from switchboard.workflows.spec import WorkflowDefinition

    definition = WorkflowDefinition(
        name="careful", prompt="do {request}", permission_mode="manual"
    )
    assert definition.permission_mode == "manual"


def test_effort_is_per_role_and_falls_back_to_general():
    config = Config.model_validate({"effort": {"general": "medium", "reviewer": "high"}})
    assert config.effort_for_role("reviewer") == "high"
    assert config.effort_for_role("implementer") == "medium"
    assert config.effort_for_role("a-role-nobody-declared") == "medium"


def _overlay(store, tmp_path: Path, config: Config, *, read_only: bool = False) -> dict:
    """The settings file a launch would hand to `claude --settings`."""
    import json

    native = NativeClaudeRuntime(store, _Recorder(), config, tmp_path / "hooks")
    runtime_id = uuid4()
    return json.loads(native._write_settings(runtime_id, read_only=read_only).read_text())


def test_a_worker_is_granted_no_commands_by_default(store, tmp_path):
    """The default posture is Claude's: every command asks."""
    assert "permissions" not in _overlay(store, tmp_path, Config())


def test_configured_commands_reach_a_writable_workers_settings_overlay(store, tmp_path):
    """The overlay is the only channel that reaches a worker.

    A worker runs in a worktree Claude has never been asked to trust, and Claude ignores
    `permissions.allow` from an untrusted directory's own settings. So a rule checked into
    the repository does nothing for a worker; only what Switchboard passes does.
    """
    config = Config.model_validate(
        {"permissions": {"writable_worker_allow": ["Bash(./.venv/bin/python -m pytest:*)"]}}
    )
    overlay = _overlay(store, tmp_path, config)
    assert overlay["permissions"]["allow"] == ["Bash(./.venv/bin/python -m pytest:*)"]
    assert overlay["hooks"], "the hook bridge must survive alongside the rules"


def test_a_read_only_worker_is_granted_nothing(store, tmp_path):
    config = Config.model_validate({"permissions": {"writable_worker_allow": ["Bash(git push:*)"]}})
    assert "permissions" not in _overlay(store, tmp_path, config, read_only=True)
