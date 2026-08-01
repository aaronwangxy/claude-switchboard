"""The manager's context is CSM's own state -- never a repository's.

If the manager inherited the CLAUDE.md of whichever repository CSM happened to be
launched from, it would stop being a router and start behaving like that repository's
coding agent. These assertions are about that boundary, not about model behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from switchboard.agents.manager import MANAGER_TOOL_NAMES, ModelManager
from switchboard.config import Config

pytest.importorskip("claude_agent_sdk")


@pytest.fixture
def options(session_manager, monkeypatch, git_repo):
    """Manager options built while the process is sitting inside a real repository."""
    repo = git_repo("noisy")
    (repo / "CLAUDE.md").write_text("# Always edit files immediately.\n")
    session_manager.register_repository(repo)
    monkeypatch.chdir(repo)
    return ModelManager(session_manager).options()


def test_the_manager_loads_no_setting_sources(options):
    assert options.setting_sources == []


def test_the_manager_runs_in_the_csm_data_directory(options, session_manager):
    assert Path(options.cwd) == session_manager.store.path.parent


def test_the_manager_has_no_file_shell_or_subagent_tools(options):
    for denied in ("Bash", "Edit", "Write", "Read", "Glob", "Grep", "Task"):
        assert denied in options.disallowed_tools
    assert all(name.startswith("mcp__switchboard__") for name in options.allowed_tools)
    assert set(options.allowed_tools) == {f"mcp__switchboard__{name}" for name in MANAGER_TOOL_NAMES}


def test_the_manager_turn_is_bounded(options):
    assert options.max_turns == 12


def test_the_configured_executable_and_env_reach_the_manager(session_manager, tmp_path):
    import stat

    wrapper = tmp_path / "company-claude"
    wrapper.write_text("#!/bin/sh\nexit 0\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    config = Config()
    config.claude.executable = str(wrapper)
    config.claude.env = {"COMPANY_PROXY": "on"}
    session_manager.config = config

    options = ModelManager(session_manager).options()
    assert Path(options.cli_path) == wrapper
    assert options.env == {"COMPANY_PROXY": "on"}


def test_workers_by_contrast_do_inherit_repository_settings(session_manager):
    """The mirror image of the invariant above: workers *should* see the repository."""
    assert session_manager.config.setting_sources == ["user", "project"]
