"""Workflow discovery.

Workflows are YAML. The built-ins ship inside the package and are loaded by exactly the
same code as a workflow the user drops into `~/.csm/workflows` -- there is no privileged
built-in path. Later sources override earlier ones by name, so a user can replace a
built-in without editing CSM.

Layout, either form:

    ~/.csm/workflows/post-rebase-verify.yaml
    ~/.csm/workflows/post-rebase-verify/workflow.yaml
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from pathlib import Path

import yaml
from pydantic import ValidationError

from csm.workflows.spec import WorkflowDefinition

log = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).parent / "builtin"

#: Where a repository may keep workflows of its own.
REPO_WORKFLOW_DIR = ".csm/workflows"


class WorkflowLoadError(ValueError):
    """A workflow file could not be read or validated."""


def workflow_files(directory: Path) -> Iterator[Path]:
    """Every workflow file in a directory, in a stable order."""
    if not directory.is_dir():
        return
    for entry in sorted(directory.iterdir()):
        if entry.is_file() and entry.suffix in (".yaml", ".yml"):
            yield entry
        elif entry.is_dir():
            for candidate in ("workflow.yaml", "workflow.yml"):
                if (entry / candidate).is_file():
                    yield entry / candidate
                    break


def load_file(path: Path, source: str) -> WorkflowDefinition:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise WorkflowLoadError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowLoadError(f"{path}: a workflow file must contain a YAML mapping.")
    data.setdefault("name", path.parent.name if path.stem == "workflow" else path.stem)
    data["source"] = source
    try:
        return WorkflowDefinition.model_validate(data)
    except ValidationError as exc:
        raise WorkflowLoadError(f"{path}: {exc.error_count()} problem(s): {exc}") from exc


def load_directory(
    directory: Path, source: str, into: dict[str, WorkflowDefinition], problems: list[str]
) -> None:
    """Load one directory into `into`, recording rather than raising on a bad file.

    One broken user workflow must not stop CSM from starting; it is reported instead.
    """
    for path in workflow_files(directory):
        try:
            definition = load_file(path, source)
        except WorkflowLoadError as exc:
            problems.append(str(exc))
            log.warning("skipping workflow %s: %s", path, exc)
            continue
        into[definition.name] = definition


def load_all(extra_dirs: Iterable[Path] = ()) -> tuple[dict[str, WorkflowDefinition], list[str]]:
    """Built-ins, then the user directory, then any repository directories."""
    from csm.config import user_workflows_dir

    definitions: dict[str, WorkflowDefinition] = {}
    problems: list[str] = []
    load_directory(BUILTIN_DIR, "builtin", definitions, problems)
    load_directory(user_workflows_dir(), "user", definitions, problems)
    for directory in extra_dirs:
        load_directory(Path(directory), f"repo:{Path(directory).parent.parent.name}", definitions, problems)
    return definitions, problems
