"""Declarative workflow specs: loading, overriding, aliases, and template rendering."""

from __future__ import annotations

from pathlib import Path

import pytest

from switchboard.domain.enums import ArtifactType, WorkerRole
from switchboard.workflows import loader
from switchboard.workflows.registry import (
    WORKFLOWS,
    WorkflowError,
    get_workflow,
    load_problems,
    reload_workflows,
    render_template,
    validate_for_role,
)
from switchboard.workflows.spec import Approval, StepCondition, WorkerMode, WorkflowDefinition

#: Every workflow the goal requires CSM to ship.
REQUIRED_BUILTINS = [
    "complete-ticket",
    "ask-question",
    "rebase-stack",
    "address-review-comments",
    "smoke-test",
    "independent-review",
]


def test_every_required_builtin_workflow_is_loaded():
    assert load_problems() == []
    for name in REQUIRED_BUILTINS:
        assert name in WORKFLOWS, f"{name} is missing from the registry"


def test_builtin_workflows_come_from_yaml_not_python():
    """The built-ins use the same loader as a user workflow, from the same file format."""
    files = {path.stem for path in loader.workflow_files(loader.BUILTIN_DIR)}
    assert "complete-ticket" in files
    assert all(WORKFLOWS[name].source == "builtin" for name in REQUIRED_BUILTINS)


def test_renamed_workflows_keep_their_previous_names_as_aliases():
    assert get_workflow("rereview").name == "independent-review"
    assert get_workflow("review-change").name == "independent-review"
    assert get_workflow("answer-codebase-question").name == "ask-question"


def test_an_unknown_workflow_names_the_ones_that_exist():
    with pytest.raises(WorkflowError) as excinfo:
        get_workflow("do-magic")
    assert "do-magic" in str(excinfo.value)
    assert "complete-ticket" in str(excinfo.value)


def test_complete_ticket_is_a_composite_of_reusable_steps():
    definition = get_workflow("complete-ticket")
    assert definition.is_composite
    names = [step.workflow for step in definition.steps]
    assert names[:4] == [
        "plan-feature",
        "implement-approved-plan",
        "full-verify",
        "independent-review",
    ]
    # Every referenced step must itself be a real atomic workflow.
    for step in definition.steps:
        assert not get_workflow(step.workflow).is_composite
    assert definition.steps[0].approval is Approval.REQUIRED
    fix_step = next(s for s in definition.steps if s.workflow == "address-review-comments")
    assert fix_step.when is StepCondition.BLOCKING_FINDINGS
    assert fix_step.max_iterations == 2


def test_a_composite_cannot_be_targeted_at_one_worker():
    with pytest.raises(WorkflowError):
        validate_for_role("complete-ticket", WorkerRole.IMPLEMENTER)


def test_declared_safety_fields_survive_the_yaml_round_trip():
    implement = get_workflow("implement-approved-plan")
    assert implement.mutates_code is True
    assert implement.requires == frozenset({ArtifactType.IMPLEMENTATION_CONTRACT})
    assert ArtifactType.REVIEW in implement.invalidates
    review = get_workflow("independent-review")
    assert review.mutates_code is False
    assert review.worker is WorkerMode.FRESH
    assert review.produces == frozenset({ArtifactType.REVIEW})


# ------------------------------------------------------------------- templates


def test_render_template_substitutes_known_tokens_only():
    rendered = render_template("Scope: {scope}\n{ \"a\": {\"b\": 1} }", {"scope": "smoke"})
    assert "Scope: smoke" in rendered
    assert '{ "a": {"b": 1} }' in rendered, "JSON braces must survive untouched"


def test_render_template_leaves_unknown_tokens_alone():
    assert render_template("{request} {nothing_supplied}", {"request": "x"}) == "x {nothing_supplied}"


def test_builtin_prompts_render_without_leaking_placeholders():
    values = {
        "request": "R", "artifacts": "A", "plan_max_lines": 10, "scope": "full",
        "base_ref": "main", "preserve_merges": False, "autosquash_fixups": True,
        "never_force_push": True, "base_commit": "b", "head_commit": "h",
        "commits": "c", "diff": "d",
    }
    for definition in WORKFLOWS.values():
        if definition.is_composite:
            continue
        rendered = render_template(definition.prompt, values)
        assert "{request}" not in rendered
        assert "{artifacts}" not in rendered


# ------------------------------------------------------- user-defined workflows


def _write(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(body)


CUSTOM = """\
name: post-rebase-verify
description: Rebase, regenerate snapshots, then smoke test.
role: verifier
mutates_code: false
requires: [behavior_contract]
produces: [smoke_verification]
model_role: verifier
prompt: |
  Re-run the snapshot regeneration and the smoke test.
  {request}
"""


def test_a_user_can_add_a_workflow_without_editing_csm(isolated_workflows: Path):
    _write(isolated_workflows, "post-rebase-verify.yaml", CUSTOM)
    assert reload_workflows() == []
    definition = get_workflow("post-rebase-verify")
    assert definition.source == "user"
    assert definition.role is WorkerRole.VERIFIER
    assert definition.produces == frozenset({ArtifactType.SMOKE_VERIFICATION})
    # It is allowed for its own role like any built-in.
    assert validate_for_role("post-rebase-verify", WorkerRole.VERIFIER) is definition


def test_a_workflow_directory_with_workflow_yaml_is_discovered(isolated_workflows: Path):
    _write(isolated_workflows / "my-flow", "workflow.yaml", "description: x\nprompt: 'do {request}'\n")
    assert reload_workflows() == []
    assert get_workflow("my-flow").description == "x"


def test_a_user_workflow_may_not_redefine_a_builtin(isolated_workflows: Path):
    """Built-in names are reserved -- see `loader.load_all` for why."""
    _write(isolated_workflows, "ask-question.yaml", "name: ask-question\nprompt: 'mine {request}'\n")
    problems = reload_workflows()

    assert get_workflow("ask-question").source == "builtin"
    assert get_workflow("ask-question").prompt.strip() != "mine {request}"
    assert any("built-in" in problem for problem in problems)


def test_a_broken_user_workflow_is_reported_and_skipped(isolated_workflows: Path):
    _write(isolated_workflows, "broken.yaml", "name: broken\nrole: not-a-role\nprompt: hi\n")
    _write(isolated_workflows, "fine.yaml", "name: fine\nprompt: 'ok {request}'\n")
    problems = reload_workflows()
    assert len(problems) == 1 and "broken.yaml" in problems[0]
    assert "broken" not in WORKFLOWS
    assert "fine" in WORKFLOWS, "one bad file must not stop the rest from loading"
    assert "complete-ticket" in WORKFLOWS


def test_a_workflow_must_be_either_a_prompt_or_steps():
    with pytest.raises(ValueError):
        WorkflowDefinition(name="x", prompt="hi", steps=[{"workflow": "ask-question"}])
    with pytest.raises(ValueError):
        WorkflowDefinition(name="x")
