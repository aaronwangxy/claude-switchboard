"""Safety invariants of `WorktreeService` that need no real worktree."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

from csm.domain.enums import WorkerRole
from csm.domain.models import Job, Repository, Worker
from csm.gitops.worktrees import WorktreeSafetyError, WorktreeService, slug


def make_repository(path: Path, name: str = "demo") -> Repository:
    return Repository(name=name, root_path=path, default_branch="main")


def make_worker(repository: Repository) -> Worker:
    return Worker(
        title="implement thing",
        role=WorkerRole.IMPLEMENTER,
        repository_id=repository.id,
        cwd=repository.root_path,
        writable=True,
    )


# --------------------------------------------------------------------- validate_path


def test_validate_path_accepts_paths_inside_the_managed_root(worktree_service: WorktreeService):
    inside = worktree_service.root / "demo" / "job-implementer-abc"
    assert worktree_service.validate_path(inside) == inside.resolve()


def test_validate_path_accepts_deeply_nested_paths(worktree_service: WorktreeService):
    nested = worktree_service.root / "a" / "b" / "c"
    assert worktree_service.validate_path(nested) == nested.resolve()


def test_validate_path_refuses_the_managed_root_itself(worktree_service: WorktreeService):
    with pytest.raises(WorktreeSafetyError, match="outside the managed worktree root"):
        worktree_service.validate_path(worktree_service.root)


def test_validate_path_refuses_the_parent_of_the_root(worktree_service: WorktreeService):
    with pytest.raises(WorktreeSafetyError):
        worktree_service.validate_path(worktree_service.root.parent)


def test_validate_path_refuses_an_unrelated_absolute_path(
    worktree_service: WorktreeService, tmp_path: Path
):
    with pytest.raises(WorktreeSafetyError):
        worktree_service.validate_path(tmp_path / "somebody-elses-repo")


def test_validate_path_refuses_traversal_back_out_of_the_root(worktree_service: WorktreeService):
    escaping = worktree_service.root / "demo" / ".." / ".." / "escaped"
    with pytest.raises(WorktreeSafetyError):
        worktree_service.validate_path(escaping)


# ------------------------------------------------------- assert_single_writable_owner


def test_assert_single_writable_owner_rejects_a_second_writable_worker():
    worktree_id, owner, candidate = uuid4(), uuid4(), uuid4()
    with pytest.raises(WorktreeSafetyError) as excinfo:
        WorktreeService.assert_single_writable_owner(worktree_id, owner, candidate)
    message = str(excinfo.value)
    assert str(worktree_id) in message
    assert str(owner) in message


def test_assert_single_writable_owner_is_a_noop_for_the_same_worker():
    worker_id = uuid4()
    assert (
        WorktreeService.assert_single_writable_owner(uuid4(), worker_id, worker_id) is None
    )


def test_assert_single_writable_owner_is_a_noop_when_unowned():
    assert WorktreeService.assert_single_writable_owner(uuid4(), None, uuid4()) is None


# -------------------------------------------------------------------- create_worktree


def test_create_worktree_refuses_a_missing_base_ref(
    worktree_service: WorktreeService, git_repo: Callable[[str], Path]
):
    repo_path = git_repo("demo")
    repository = make_repository(repo_path)
    worker = make_worker(repository)

    with pytest.raises(WorktreeSafetyError, match="does not exist"):
        worktree_service.create_worktree(repository, None, worker, "no-such-branch")

    assert not worktree_service.root.exists() or not any(worktree_service.root.rglob(".git"))


def test_create_worktree_refuses_when_the_target_path_already_exists(
    worktree_service: WorktreeService, git_repo: Callable[[str], Path]
):
    repo_path = git_repo("demo")
    repository = make_repository(repo_path)
    job = Job(title="Add login", external_ref="ENG-1", repository_id=repository.id)
    worker = make_worker(repository)

    target = worktree_service.path_for(repository, job, worker)
    target.mkdir(parents=True)

    with pytest.raises(WorktreeSafetyError, match="already exists"):
        worktree_service.create_worktree(repository, job, worker, "main")


# ------------------------------------------------------------------------------ slug


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ENG-1234", "eng-1234"),
        ("PROJ/456: Fix the thing!", "proj-456-fix-the-thing"),
        ("  spaces  everywhere  ", "spaces-everywhere"),
        ("feature/add_login.v2", "feature-add_login.v2"),
        ("!!!", "job"),
        ("", "job"),
        # path separators collapse to dashes, so a ticket ref stays a single path component
        ("ENG-1/../../etc", "eng-1-..-..-etc"),
    ],
)
def test_slug_sanitises_ticket_like_strings(raw: str, expected: str):
    assert slug(raw) == expected


def test_slug_truncates_to_the_limit():
    assert slug("a" * 100) == "a" * 32
    assert slug("a" * 100, limit=8) == "a" * 8


def test_slug_never_contains_path_separators():
    assert "/" not in slug("a/b/c")
    assert "\\" not in slug("a\\b\\c")
