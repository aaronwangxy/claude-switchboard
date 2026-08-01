"""Which Claude executable a worker session actually launches."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from csm.agents.backend import WorkerSpec
from csm.agents.runtime import ClaudeRuntimeError, claude_cli_path


def _make_executable(path):
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class TestClaudeExecutable:
    def test_unset_lets_the_sdk_choose(self):
        assert claude_cli_path(None) is None
        assert claude_cli_path("") is None

    def test_absolute_path_is_used_as_given(self, tmp_path):
        wrapper = _make_executable(tmp_path / "company-claude")
        assert claude_cli_path(str(wrapper)) == wrapper

    def test_bare_name_is_resolved_on_path(self, tmp_path, monkeypatch):
        wrapper = _make_executable(tmp_path / "company-claude")
        monkeypatch.setenv("PATH", str(tmp_path))
        assert claude_cli_path("company-claude") == wrapper

    def test_missing_executable_is_refused_with_the_alias_hint(self):
        with pytest.raises(ClaudeRuntimeError) as exc:
            claude_cli_path("definitely-not-a-real-command-xyz")
        # The likeliest mistake is pointing this at a shell alias, so say so.
        assert "alias" in str(exc.value)

    def test_non_executable_file_is_refused(self, tmp_path):
        plain = tmp_path / "claude"
        plain.write_text("not executable")
        plain.chmod(0o644)
        with pytest.raises(ClaudeRuntimeError):
            claude_cli_path(str(plain))


def test_the_configured_executable_reaches_the_worker_spec():
    """The config value is threaded to the spec, not read again inside the backend."""
    spec = WorkerSpec(
        worker_id=uuid4(),
        role="implementer",
        cwd=Path("."),
        system_prompt_append="",
        initial_prompt="",
        claude_executable="company-claude",
        env={"COMPANY_PROXY": "on"},
    )
    assert spec.claude_executable == "company-claude"
    assert spec.env == {"COMPANY_PROXY": "on"}
    assert "COMPANY_PROXY" not in os.environ  # the spec never mutates the process
