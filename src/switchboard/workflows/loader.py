"""Workflow discovery.

Workflows are YAML. The built-ins ship inside the package and are loaded by exactly the
same code as a workflow the user drops into `~/.switchboard/workflows` -- adding one requires no
change to Switchboard.

Built-in *names* are reserved, though. A workflow's declarations are load-bearing:
`requires` is what stops implementation running without an approved contract, and
`mutates_code` is what decides whether a worker gets an isolated worktree at all. Every
field defaults to permissive, so a file that merely reuses a built-in's name -- without
stating anything -- would silently strip both. A repository's workflows travel with its
clone, which would put that file inside the repository it is meant to constrain.

Layout, either form:

    ~/.switchboard/workflows/post-rebase-verify.yaml
    ~/.switchboard/workflows/post-rebase-verify/workflow.yaml
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from pathlib import Path

import yaml
from pydantic import ValidationError

from switchboard.workflows.spec import WorkflowDefinition

log = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).parent / "builtin"

#: Where a repository may keep workflows of its own.
REPO_WORKFLOW_DIR = ".switchboard/workflows"


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
    directory: Path,
    source: str,
    into: dict[str, WorkflowDefinition],
    problems: list[str],
    reserved: frozenset[str] = frozenset(),
) -> None:
    """Load one directory into `into`, recording rather than raising on a bad file.

    One broken user workflow must not stop Switchboard from starting; it is reported instead.
    A file claiming a reserved name is refused the same way, so the built-in it would
    have replaced stays in force.
    """
    for path in workflow_files(directory):
        try:
            definition = load_file(path, source)
        except WorkflowLoadError as exc:
            problems.append(str(exc))
            log.warning("skipping workflow %s: %s", path, exc)
            continue
        if definition.name in reserved:
            problems.append(
                f"{path}: {definition.name!r} is a built-in workflow and cannot be "
                "redefined. Choose another name."
            )
            log.warning("refusing to redefine built-in workflow %s from %s", definition.name, path)
            continue
        into[definition.name] = definition


def load_all(extra_dirs: Iterable[Path] = ()) -> tuple[dict[str, WorkflowDefinition], list[str]]:
    """Built-ins, then the user directory, then any repository directories.

    Later sources add to earlier ones but may never replace a built-in. Among themselves
    they still override by name, which is how a repository states a convention its
    contributors should get over the user's own version of the same idea.
    """
    from switchboard.config import user_workflows_dir

    definitions: dict[str, WorkflowDefinition] = {}
    problems: list[str] = []
    load_directory(BUILTIN_DIR, "builtin", definitions, problems)
    reserved = frozenset(definitions)
    load_directory(user_workflows_dir(), "user", definitions, problems, reserved)
    for directory in extra_dirs:
        source = f"repo:{Path(directory).parent.parent.name}"
        load_directory(Path(directory), source, definitions, problems, reserved)
    return definitions, problems


def builtin_names() -> frozenset[str]:
    """The names no user or repository workflow may claim."""
    definitions: dict[str, WorkflowDefinition] = {}
    load_directory(BUILTIN_DIR, "builtin", definitions, [])
    return frozenset(definitions)
