"""The workflow registry.

One live dict of loaded workflows, refreshed from disk on demand. Everything that needs a
workflow goes through `get_workflow`, so a user-defined workflow is indistinguishable from
a built-in one at every call site.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from csm.domain.enums import WorkerRole
from csm.workflows.loader import load_all
from csm.workflows.spec import (
    Approval,
    StepCondition,
    WorkerMode,
    WorkflowDefinition,
    WorkflowError,
    WorkflowStep,
    render_template,
)

__all__ = [
    "WORKFLOWS",
    "Approval",
    "StepCondition",
    "WorkerMode",
    "WorkflowDefinition",
    "WorkflowError",
    "WorkflowStep",
    "get_workflow",
    "load_problems",
    "reload_workflows",
    "render_template",
    "validate_for_role",
    "workflow_names",
]

#: Canonical name -> definition. Mutated in place by `reload_workflows`, so modules that
#: imported it keep seeing the current registry.
WORKFLOWS: dict[str, WorkflowDefinition] = {}

#: Alias -> canonical name, declared by each workflow's `aliases:`.
_ALIASES: dict[str, str] = {}

_PROBLEMS: list[str] = []


def reload_workflows(extra_dirs: Iterable[Path] = ()) -> list[str]:
    """Reload every workflow source. Returns user-visible problems, never raises."""
    definitions, problems = load_all(extra_dirs)
    WORKFLOWS.clear()
    WORKFLOWS.update(definitions)
    _ALIASES.clear()
    for definition in definitions.values():
        for alias in definition.aliases:
            if alias not in definitions:
                _ALIASES[alias] = definition.name
    _PROBLEMS[:] = problems
    return problems


def load_problems() -> list[str]:
    return list(_PROBLEMS)


def workflow_names() -> list[str]:
    return sorted(WORKFLOWS)


def get_workflow(name: str) -> WorkflowDefinition:
    resolved = _ALIASES.get(name, name)
    try:
        return WORKFLOWS[resolved]
    except KeyError:
        raise WorkflowError(
            f"Unknown workflow {name!r}. Known workflows: {', '.join(workflow_names())}."
        ) from None


def validate_for_role(name: str, role: WorkerRole) -> WorkflowDefinition:
    definition = get_workflow(name)
    if definition.is_composite:
        raise WorkflowError(
            f"Workflow {name!r} is composite; its steps run on their own workers, so it "
            "cannot be targeted at one existing worker."
        )
    if role not in definition.allowed_roles:
        raise WorkflowError(
            f"Workflow {name!r} cannot run on a {role.value} worker; "
            f"allowed roles: {', '.join(sorted(r.value for r in definition.allowed_roles))}."
        )
    return definition


reload_workflows()
