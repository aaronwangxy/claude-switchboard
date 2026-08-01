"""Workflows that travel with a repository.

A team convention belongs to the repository, not to whichever machine happens to be
running CSM, so registering a repository picks up its `.csm/workflows` directory.
"""

from __future__ import annotations

from pathlib import Path

from switchboard.core.session_manager import SessionManager
from switchboard.workflows.registry import REPO_WORKFLOW_DIR, get_workflow, workflow_names

SPEC = """
name: house-review
description: The review this repository always runs.
role: reviewer
produces: [review]
prompt: |
  Review {job_title} the way this repository expects.
"""


def _write_workflow(repo: Path, name: str, body: str) -> None:
    directory = repo / REPO_WORKFLOW_DIR
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.yaml").write_text(body)


def test_registering_a_repository_loads_its_workflows(session_manager, git_repo):
    repo = git_repo("with-workflows")
    _write_workflow(repo, "house-review", SPEC)
    assert "house-review" not in workflow_names()

    session_manager.register_repository(repo)

    assert "house-review" in workflow_names()
    assert get_workflow("house-review").description == "The review this repository always runs."


def test_a_repository_may_not_redefine_a_builtin(session_manager, git_repo):
    """A repository's own file must not be able to strip a built-in's declarations.

    `requires` is what stops implementation running without an approved contract, and
    `mutates_code` is what decides whether a worker is isolated in a worktree at all.
    Both default to permissive, so a file that merely reuses the name would remove them
    -- from inside the very repository they exist to constrain.
    """
    repo = git_repo("overriding")
    _write_workflow(
        repo,
        "implement-approved-plan",
        "name: implement-approved-plan\ndescription: No contract needed here.\n"
        "role: implementer\nprompt: Just implement {job_title}.\n",
    )
    session_manager.register_repository(repo)
    problems = session_manager.reload_workflows()

    definition = get_workflow("implement-approved-plan")
    assert definition.description != "No contract needed here."
    assert definition.requires  # the built-in's prerequisites survive
    assert definition.mutates_code
    assert any("built-in" in problem for problem in problems)


def test_one_repository_workflow_does_not_leak_its_name_into_a_builtin(
    session_manager, git_repo
):
    repo = git_repo("adding")
    _write_workflow(repo, "house-review", SPEC)
    session_manager.register_repository(repo)
    # A new name is added; every built-in is still exactly what CSM shipped.
    assert get_workflow("house-review").source.startswith("repo:")
    assert get_workflow("independent-review").source == "builtin"


def test_a_repository_without_workflows_changes_nothing(session_manager, git_repo):
    before = set(workflow_names())
    session_manager.register_repository(git_repo("plain"))
    assert set(workflow_names()) == before


def test_a_broken_repository_workflow_is_reported_not_raised(session_manager, git_repo):
    repo = git_repo("broken")
    _write_workflow(repo, "bad", "name: bad\nsteps: [{workflow: x}]\nprompt: also a prompt\n")
    session_manager.register_repository(repo)  # must not raise
    problems = session_manager.reload_workflows()
    assert any("bad" in problem for problem in problems)
    assert "bad" not in workflow_names()


def test_reload_survives_a_repository_directory_that_disappeared(
    session_manager: SessionManager, git_repo, tmp_path
):
    repo = git_repo("vanishing")
    _write_workflow(repo, "house-review", SPEC)
    session_manager.register_repository(repo)
    assert "house-review" in workflow_names()

    (repo / REPO_WORKFLOW_DIR / "house-review.yaml").unlink()
    (repo / REPO_WORKFLOW_DIR).rmdir()

    assert session_manager.reload_workflows() == []
    assert "house-review" not in workflow_names()
