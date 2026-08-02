"""Static checks on the loaded workflow registry.

Borrowed from the AWS CLI Agent Orchestrator: an authoring mistake should be found before
somebody's work depends on it, not three steps into a run. Everything here is answerable
from the definitions alone -- no repository, no job, no model.
"""

from __future__ import annotations

from dataclasses import dataclass

from switchboard.core.evidence import required_artifacts
from switchboard.workflows.registry import (
    WORKFLOWS,
    find_workflow,
    get_workflow,
    load_problems,
    workflow_names,
)
from switchboard.workflows.spec import WorkflowDefinition


@dataclass(frozen=True)
class Problem:
    workflow: str
    message: str

    def __str__(self) -> str:
        return f"{self.workflow}: {self.message}"


def _unknown_steps(definition: WorkflowDefinition) -> list[str]:
    return [step.workflow for step in definition.steps if find_workflow(step.workflow) is None]


def _cycle(name: str, seen: tuple[str, ...] = ()) -> list[str] | None:
    """A composite that reaches itself would never terminate."""
    if name in seen:
        return [*seen, name]
    definition = find_workflow(name)
    if definition is None:
        return None
    for step in definition.steps:
        found = _cycle(step.workflow, (*seen, name))
        if found is not None:
            return found
    return None


def _unsatisfiable(definition: WorkflowDefinition) -> list[Problem]:
    """A step needing an artifact no earlier step produces can only ever pause the run."""
    problems: list[Problem] = []
    available: set = set()
    for index, step in enumerate(definition.steps, start=1):
        step_definition = find_workflow(step.workflow)
        if step_definition is None:
            continue
        missing = sorted(a.value for a in step_definition.requires - available)
        if missing:
            producers = sorted(
                other
                for other in workflow_names()
                if any(a.value in missing for a in get_workflow(other).produces)
            )
            problems.append(
                Problem(
                    definition.name,
                    f"step {index} ({step.workflow}) needs {', '.join(missing)}, which no "
                    f"earlier step produces. Add one of: {', '.join(producers) or 'nothing'}"
                    ", or link a job that has it as context.",
                )
            )
        available |= step_definition.produces
    return problems


def _atomic_problems(definition: WorkflowDefinition) -> list[Problem]:
    problems: list[Problem] = []
    if definition.role not in definition.allowed_roles:
        problems.append(
            Problem(
                definition.name,
                f"its own role {definition.role.value!r} is not in allowed_roles; no worker "
                "it starts could run it.",
            )
        )
    if definition.produces and not definition.prompt.strip():
        problems.append(Problem(definition.name, "promises artifacts but has no prompt."))
    # An unknown `{token}` is deliberately *not* a problem: `render_template` leaves every
    # brace it was not given alone, which is why a prompt can hold a JSON schema or
    # `git rev-parse HEAD^{tree}` without escaping. Flagging those would report the
    # built-ins as broken.
    if definition.requires & definition.produces:
        overlap = sorted(a.value for a in definition.requires & definition.produces)
        problems.append(
            Problem(definition.name, f"both requires and produces {', '.join(overlap)}.")
        )
    return problems


def validate_registry() -> list[Problem]:
    """Every problem in the loaded workflows, in a stable order."""
    problems = [Problem("(loading)", message) for message in load_problems()]
    for name in workflow_names():
        definition = WORKFLOWS[name]
        if definition.is_composite:
            for unknown in _unknown_steps(definition):
                problems.append(
                    Problem(name, f"step {unknown!r} names a workflow that does not exist.")
                )
            cycle = _cycle(name)
            if cycle is not None:
                problems.append(Problem(name, "composes itself: " + " -> ".join(cycle)))
            elif not _unknown_steps(definition):
                problems.extend(_unsatisfiable(definition))
                if not required_artifacts(definition):
                    problems.append(
                        Problem(
                            name,
                            "no unconditional step produces anything, so a job following it "
                            "can never be reported complete.",
                        )
                    )
        else:
            problems.extend(_atomic_problems(definition))
    return problems
