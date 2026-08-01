"""End-to-end worktree behaviour against real temporary git repositories."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from csm.domain.enums import WorkerRole
from csm.domain.models import Job, Repository, Worker, Worktree
from csm.gitops import runner
from csm.gitops.worktrees import WorktreeSafetyError, WorktreeService


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout


def make_repository(path: Path, name: str = "demo") -> Repository:
    return Repository(name=name, root_path=path, default_branch="main")


def make_worker(repository: Repository, title: str = "implement thing") -> Worker:
    return Worker(
        title=title,
        role=WorkerRole.IMPLEMENTER,
        repository_id=repository.id,
        cwd=repository.root_path,
        writable=True,
    )


def commit_file(worktree_path: Path, name: str, body: str, message: str) -> None:
    (worktree_path / name).write_text(body)
    git(worktree_path, "add", name)
    git(worktree_path, "commit", "--quiet", "-m", message)


@pytest.fixture
def repository(git_repo: Callable[[str], Path]) -> Repository:
    return make_repository(git_repo("demo"))


@pytest.fixture
def job(repository: Repository) -> Job:
    return Job(title="Add login", external_ref="ENG-1234", repository_id=repository.id)


# ---------------------------------------------------------------------------- create


def test_create_worktree_makes_a_real_worktree_outside_the_source_repo(
    worktree_service: WorktreeService, repository: Repository, job: Job
):
    worker = make_worker(repository)

    worktree = worktree_service.create_worktree(repository, job, worker, "main")

    assert worktree.path.exists()
    assert (worktree.path / ".git").exists()
    assert (worktree.path / "README.md").read_text() == "# demo\n"

    # a brand new branch, checked out in the new worktree
    assert worktree.branch.startswith("csm/eng-1234-")
    assert runner.current_branch(worktree.path) == worktree.branch
    assert worktree.base_ref == "main"
    assert worktree.owner_worker_id == worker.id
    assert worktree.repository_id == repository.id

    # the parent repo knows about it
    listing = runner.worktree_list(repository.root_path)
    assert str(worktree.path.resolve()) in listing

    # it lives under the managed root, never inside the user's repository
    resolved = worktree.path.resolve()
    assert worktree_service.root.resolve() in resolved.parents
    assert repository.root_path.resolve() not in resolved.parents
    assert worktree_service.validate_path(worktree.path) == resolved


def test_two_writable_workers_get_separate_worktrees_and_branches(
    worktree_service: WorktreeService, repository: Repository, job: Job
):
    first = worktree_service.create_worktree(repository, job, make_worker(repository), "main")
    second = worktree_service.create_worktree(repository, job, make_worker(repository), "main")

    assert first.path != second.path
    assert first.branch != second.branch
    assert first.path.exists() and second.path.exists()

    listing = runner.worktree_list(repository.root_path)
    assert str(first.path.resolve()) in listing
    assert str(second.path.resolve()) in listing

    # edits in one worktree are invisible to the other
    (first.path / "only-in-first.txt").write_text("hello\n")
    assert not (second.path / "only-in-first.txt").exists()


def test_worktree_for_an_adhoc_worker_without_a_job(
    worktree_service: WorktreeService, repository: Repository
):
    worker = make_worker(repository)
    worktree = worktree_service.create_worktree(repository, None, worker, "main")

    assert "adhoc" in worktree.path.name
    assert worktree.branch.startswith("csm/adhoc-")
    assert worktree.path.exists()


# --------------------------------------------------------------------------- dirty


def test_uncommitted_changes_block_cleanup_and_survive(
    worktree_service: WorktreeService, repository: Repository, job: Job
):
    worktree = worktree_service.create_worktree(repository, job, make_worker(repository), "main")
    scratch = worktree.path / "work-in-progress.txt"
    scratch.write_text("precious unsaved work\n")

    dirty = worktree_service.get_dirty_state(worktree)
    assert any("work-in-progress.txt" in line for line in dirty)

    decision = worktree_service.can_cleanup(worktree)
    assert decision.safe is False
    assert "uncommitted" in decision.explanation
    assert "work-in-progress.txt" in decision.explanation

    result = worktree_service.cleanup_worktree(repository, worktree)
    assert result.safe is False

    # nothing was destroyed
    assert scratch.exists()
    assert scratch.read_text() == "precious unsaved work\n"
    assert str(worktree.path.resolve()) in runner.worktree_list(repository.root_path)


def test_modified_tracked_file_also_blocks_cleanup(
    worktree_service: WorktreeService, repository: Repository, job: Job
):
    worktree = worktree_service.create_worktree(repository, job, make_worker(repository), "main")
    (worktree.path / "README.md").write_text("# demo\nlocal edit\n")

    decision = worktree_service.can_cleanup(worktree, acknowledge_disposable=True)
    assert decision.safe is False
    assert "uncommitted" in decision.explanation
    assert (worktree.path / "README.md").read_text().endswith("local edit\n")


# ------------------------------------------------------------------ unmerged commits


def test_commits_ahead_of_base_block_cleanup_unless_acknowledged(
    worktree_service: WorktreeService, repository: Repository, job: Job
):
    worktree = worktree_service.create_worktree(repository, job, make_worker(repository), "main")
    commit_file(worktree.path, "feature.py", "print('hi')\n", "add feature")

    assert worktree_service.get_dirty_state(worktree) == []

    refused = worktree_service.can_cleanup(worktree)
    assert refused.safe is False
    assert "not reachable" in refused.explanation
    assert worktree.base_ref in refused.explanation

    assert worktree_service.cleanup_worktree(repository, worktree).safe is False
    assert worktree.path.exists()

    acknowledged = worktree_service.cleanup_worktree(
        repository, worktree, acknowledge_disposable=True
    )
    assert acknowledged.safe is True
    assert not worktree.path.exists()
    # the commits are still reachable from the preserved branch
    assert worktree.branch in git(repository.root_path, "branch", "--list", worktree.branch)


def test_get_unpushed_commits_returns_commits_ahead_of_base(
    worktree_service: WorktreeService, repository: Repository, job: Job
):
    worktree = worktree_service.create_worktree(repository, job, make_worker(repository), "main")
    assert worktree_service.get_unpushed_commits(worktree) == []

    commit_file(worktree.path, "one.txt", "1\n", "first change")
    commit_file(worktree.path, "two.txt", "2\n", "second change")

    commits = worktree_service.get_unpushed_commits(worktree)
    assert len(commits) == 2
    # newest first, as `git log` reports it
    assert "second change" in commits[0]
    assert "first change" in commits[1]

    state = worktree_service.inspect_worktree(worktree)
    assert state.exists is True
    assert state.dirty is False
    assert state.branch == worktree.branch
    assert state.head == runner.head_commit(worktree.path)
    assert len(state.unpushed_commits) == 2


# -------------------------------------------------------------------------- cleanup


def test_clean_worktree_is_removed_but_its_branch_is_preserved(
    worktree_service: WorktreeService, repository: Repository, job: Job
):
    worktree = worktree_service.create_worktree(repository, job, make_worker(repository), "main")
    branch = worktree.branch
    assert str(worktree.path.resolve()) in runner.worktree_list(repository.root_path)

    decision = worktree_service.cleanup_worktree(repository, worktree)

    assert decision.safe is True
    assert not worktree.path.exists()
    assert str(worktree.path.resolve()) not in runner.worktree_list(repository.root_path)
    # cleanup must never delete branches
    assert branch in git(repository.root_path, "branch", "--list", branch)
    assert runner.ref_exists(repository.root_path, branch)


def test_cleanup_worktree_is_idempotent(
    worktree_service: WorktreeService, repository: Repository, job: Job
):
    worktree = worktree_service.create_worktree(repository, job, make_worker(repository), "main")

    assert worktree_service.cleanup_worktree(repository, worktree).safe is True
    second = worktree_service.cleanup_worktree(repository, worktree)

    assert second.safe is True
    assert not worktree.path.exists()
    assert worktree.branch in git(repository.root_path, "branch", "--list", worktree.branch)


def test_cleanup_refuses_a_worktree_outside_the_managed_root(
    worktree_service: WorktreeService, repository: Repository
):
    """A worktree record pointing at the user's own checkout must never be removed."""
    rogue = Worktree(
        repository_id=repository.id,
        path=repository.root_path,
        branch="main",
        base_ref="main",
    )

    with pytest.raises(WorktreeSafetyError):
        worktree_service.cleanup_worktree(repository, rogue, acknowledge_disposable=True)

    assert (repository.root_path / "README.md").exists()


def test_inspect_worktree_reports_a_missing_directory(
    worktree_service: WorktreeService, repository: Repository, job: Job
):
    worktree = worktree_service.create_worktree(repository, job, make_worker(repository), "main")
    worktree_service.cleanup_worktree(repository, worktree)

    state = worktree_service.inspect_worktree(worktree)
    assert state.exists is False
    assert state.dirty is False
    assert worktree_service.get_head(worktree) is None


def test_creating_a_worktree_bootstraps_the_configured_files(
    csm_home, git_repo, session_manager
):
    """The wiring, not just the policy: a real worktree gets the real file."""
    from csm.domain.enums import WorkerRole
    from csm.domain.models import Worker
    from csm.gitops.worktrees import WorktreeService

    repo_path = git_repo("bootstrapped")
    (repo_path / "CLAUDE.local.md").write_text("# local rules\n")
    (repo_path / ".env").write_text("SECRET=1")
    repo = session_manager.register_repository(repo_path)

    service = WorktreeService(csm_home / "worktrees", ["CLAUDE.local.md"])
    worker = Worker(
        title="Implement", role=WorkerRole.IMPLEMENTER, repository_id=repo.id,
        cwd=repo.root_path, writable=True,
    )
    worktree = service.create_worktree(repo, None, worker, repo.default_branch)

    assert (worktree.path / "CLAUDE.local.md").read_text() == "# local rules\n"
    assert not (worktree.path / ".env").exists()
