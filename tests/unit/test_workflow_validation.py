"""`sb workflows validate`: authoring mistakes are found before work depends on them."""

from __future__ import annotations

from pathlib import Path

from switchboard.workflows.registry import reload_workflows
from switchboard.workflows.validate import validate_registry


def _write(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(body)


def _problems(directory: Path, name: str, body: str) -> list[str]:
    _write(directory, name, body)
    reload_workflows()
    return [str(problem) for problem in validate_registry()]


def test_the_shipped_workflows_are_clean():
    reload_workflows()
    assert validate_registry() == []


def test_a_step_naming_a_workflow_that_does_not_exist(isolated_workflows: Path):
    problems = _problems(
        isolated_workflows, "typo.yaml", "name: typo\nsteps:\n  - workflow: plan-featrue\n"
    )
    assert any("plan-featrue" in p and "does not exist" in p for p in problems)


def test_a_composite_that_composes_itself(isolated_workflows: Path):
    problems = _problems(
        isolated_workflows, "loopy.yaml", "name: loopy\nsteps:\n  - workflow: loopy\n"
    )
    assert any("composes itself" in p for p in problems)


def test_a_step_needing_evidence_no_earlier_step_produces(isolated_workflows: Path):
    """This is a run that could only ever pause on its own prerequisite check."""
    problems = _problems(
        isolated_workflows,
        "backwards.yaml",
        "name: backwards\nsteps:\n  - workflow: implement-fix\n  - workflow: investigate-issue\n",
    )
    assert any("needs findings" in p and "no earlier step produces" in p for p in problems)
    assert any("investigate-issue" in p for p in problems), "it names what would fix it"


def test_a_composite_that_could_never_be_reported_complete(isolated_workflows: Path):
    problems = _problems(
        isolated_workflows,
        "pointless.yaml",
        "name: pointless\nsteps:\n  - workflow: finalize-change\n",
    )
    assert any("can never be reported complete" in p for p in problems)


def test_a_workflow_no_worker_could_ever_run(isolated_workflows: Path):
    problems = _problems(
        isolated_workflows,
        "impossible.yaml",
        "name: impossible\nrole: verifier\nallowed_roles: [reviewer]\nprompt: 'do {request}'\n",
    )
    assert any("not in allowed_roles" in p for p in problems)


def test_a_workflow_that_requires_what_it_produces(isolated_workflows: Path):
    problems = _problems(
        isolated_workflows,
        "circular.yaml",
        "name: circular\nrequires: [review]\nproduces: [review]\nrole: reviewer\n"
        "prompt: 'do {request}'\n",
    )
    assert any("both requires and produces review" in p for p in problems)


def test_a_file_that_will_not_load_is_reported_too(isolated_workflows: Path):
    problems = _problems(
        isolated_workflows, "bad.yaml", "name: bad\nrole: 'Not A Role'\nprompt: hi\n"
    )
    assert any("bad.yaml" in p for p in problems)
