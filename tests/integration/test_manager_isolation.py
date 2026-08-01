"""Native manager launch isolation is structural rather than prompt-only."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from switchboard.agents.manager_mcp import TOOL_SCHEMAS
from switchboard.agents.native_backend import NativeClaudeBackend
from switchboard.agents.native_manager import PersistentNativeManager
from switchboard.config import Config


def fake_backend(tmp_path: Path):
    backend = object.__new__(NativeClaudeBackend)
    backend.controller = SimpleNamespace(socket_path=tmp_path / "runtime" / "tmux.sock")
    return backend


def test_manager_has_dedicated_non_repository_workspace(session_manager, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    backend = fake_backend(tmp_path)
    manager = PersistentNativeManager(session_manager, backend, state)  # type: ignore[arg-type]
    assert manager.workspace == state / "manager-workspace"
    assert not (manager.workspace / ".git").exists()
    assert manager.workspace != Path.cwd()


def test_manager_mcp_config_is_generation_bound(session_manager, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    backend = fake_backend(tmp_path)
    manager = PersistentNativeManager(session_manager, backend, state)  # type: ignore[arg-type]
    from switchboard.domain.enums import RuntimeAgentKind
    from switchboard.domain.models import RuntimeInstance

    runtime = RuntimeInstance(
        agent_id=manager.manager_id, agent_kind=RuntimeAgentKind.MANAGER, backend="native"
    )
    session_manager.store.save_runtime(runtime)
    manager._write_mcp_config(runtime)
    config = json.loads(manager._mcp_path(runtime).read_text())
    command = config["mcpServers"]["switchboard"]
    joined = " ".join(command["args"])
    assert str(runtime.id) in joined and str(runtime.generation) in joined
    assert set(TOOL_SCHEMAS)


def test_manager_launch_disables_coding_tools(session_manager, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    backend = fake_backend(tmp_path)
    manager = PersistentNativeManager(session_manager, backend, state)  # type: ignore[arg-type]
    from switchboard.domain.enums import RuntimeAgentKind
    from switchboard.domain.models import RuntimeInstance

    runtime = RuntimeInstance(
        agent_id=manager.manager_id, agent_kind=RuntimeAgentKind.MANAGER, backend="native"
    )
    args = manager._extra_args(runtime)
    assert "--strict-mcp-config" in args
    assert "--tools" in args and args[args.index("--tools") + 1] == ""
    denied = args[args.index("--disallowedTools") + 1]
    assert all(name in denied for name in ("Bash", "Edit", "Write", "Read", "Task"))
    assert "mcp__switchboard__*" in args


def test_configured_wrapper_and_environment_are_shared_with_native_runtime(
    session_manager, tmp_path
):
    wrapper = tmp_path / "company-claude"
    wrapper.write_text("#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o700)
    config = Config()
    config.claude.executable = str(wrapper)
    config.claude.env = {"COMPANY_PROXY": "on"}
    session_manager.config = config
    assert session_manager.config.claude.executable == str(wrapper)
    assert session_manager.config.claude.env == {"COMPANY_PROXY": "on"}
