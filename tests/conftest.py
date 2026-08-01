"""Shared fixtures.

Everything is rooted in a per-test temporary `CSM_HOME`, and every git operation runs
against a real temporary repository -- git is never mocked.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from csm.agents.scripted_backend import ScriptedWorkerBackend
from csm.config import HOME_ENV, WORKFLOWS_ENV, Config
from csm.core.session_manager import SessionManager
from csm.gitops.worktrees import WorktreeService
from csm.storage.store import Store
from csm.workflows.registry import reload_workflows


@pytest.fixture(autouse=True)
def isolated_workflows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the user workflow directory at a throwaway path for every test.

    Without this a test run would load whatever workflows the developer happens to have
    in `~/.csm/workflows`.
    """
    directory = tmp_path / "user-workflows"
    monkeypatch.setenv(WORKFLOWS_ENV, str(directory))
    reload_workflows()
    try:
        yield directory
    finally:
        monkeypatch.delenv(WORKFLOWS_ENV, raising=False)
        reload_workflows()


@pytest.fixture
def csm_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary CSM data directory, exported as `CSM_HOME` for the test."""
    home = tmp_path / "csm-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(HOME_ENV, str(home))
    return home


@pytest.fixture
def store(csm_home: Path) -> Iterator[Store]:
    """A `Store` on a throwaway database inside `csm_home`."""
    store = Store(csm_home / "csm.db")
    try:
        yield store
    finally:
        store.close()


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Callable[[str], Path]:
    """Factory creating a real git repository with one commit on `main`."""
    repos_root = tmp_path / "repos"

    def make_repo(name: str) -> Path:
        path = repos_root / name
        path.mkdir(parents=True)
        _git(path, "init", "-b", "main", "--quiet")
        _git(path, "config", "user.email", "csm-tests@example.com")
        _git(path, "config", "user.name", "CSM Tests")
        _git(path, "config", "commit.gpgsign", "false")
        (path / "README.md").write_text(f"# {name}\n")
        _git(path, "add", "README.md")
        _git(path, "commit", "--quiet", "-m", "initial commit")
        return path

    return make_repo


@pytest.fixture
def worktree_service(csm_home: Path) -> WorktreeService:
    """A `WorktreeService` whose managed root lives under `csm_home`."""
    return WorktreeService(root=csm_home / "worktrees")


@pytest.fixture
def backend() -> ScriptedWorkerBackend:
    """The deterministic backend, so tests exercise the real orchestration path."""
    return ScriptedWorkerBackend()


@pytest.fixture
def session_manager(
    store: Store, backend: ScriptedWorkerBackend, worktree_service: WorktreeService
) -> SessionManager:
    return SessionManager(store, backend, Config(), worktree_service)


def commit_file(cwd: Path, name: str, content: str, message: str) -> str:
    """Write, stage, and commit a file in `cwd`; returns the new HEAD."""
    (cwd / name).write_text(content)
    _git(cwd, "add", "-A")
    _git(cwd, "-c", "user.email=csm-tests@example.com", "-c", "user.name=CSM Tests",
         "commit", "--quiet", "-m", message)
    return _git(cwd, "rev-parse", "HEAD").strip()


TICKET = """ENG-421 Notification preferences

Users need per-channel notification preferences that persist across restarts.
The dispatcher must honour them for every outbound channel.
Acceptance: preferences survive a restart and the dispatcher reads them.
"""
