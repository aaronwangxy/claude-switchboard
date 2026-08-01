"""Application bootstrap: wire the services, then hand them to the UI.

Nothing here renders anything, and nothing in the UI constructs a service. The backend is
chosen once, here: `SB_BACKEND=scripted` swaps the Agent SDK for the deterministic
in-process backend and the model manager for the rule-based one, so the whole UI can be
demoed and tested offline.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from switchboard.agents.backend import WorkerBackend
from switchboard.agents.manager import DeterministicManager, Manager, ModelManager
from switchboard.config import (
    Config,
    config_path,
    database_path,
    home_dir,
    load_config,
    user_workflows_dir,
    worktree_root,
)
from switchboard.core.session_manager import SessionManager, SessionManagerError
from switchboard.gitops.worktrees import WorktreeService
from switchboard.storage.store import Store
from switchboard.ui.screens import SwitchboardApp
from switchboard.workflows.registry import get_workflow, workflow_names

log = logging.getLogger(__name__)

BACKEND_ENV = "SB_BACKEND"


@dataclass
class Services:
    """Everything the UI is allowed to talk to."""

    config: Config
    store: Store
    backend: WorkerBackend
    worktrees: WorktreeService
    session_manager: SessionManager
    manager: Manager
    scripted: bool

    def close(self) -> None:
        self.store.close()


def use_scripted_backend() -> bool:
    return os.getenv(BACKEND_ENV, "").strip().lower() == "scripted"


def build_services() -> Services:
    config = load_config()
    home_dir().mkdir(parents=True, exist_ok=True)
    store = Store(database_path())
    scripted = use_scripted_backend()
    backend: WorkerBackend
    if scripted:
        from switchboard.agents.scripted_backend import ScriptedWorkerBackend

        backend = ScriptedWorkerBackend()
    else:
        from switchboard.agents.sdk_backend import SdkWorkerBackend

        backend = SdkWorkerBackend()
    worktrees = WorktreeService(worktree_root(), config.worktree_bootstrap.files)
    session_manager = SessionManager(store, backend, config, worktrees)
    session_manager.reload_workflows()
    manager: Manager = (
        DeterministicManager(session_manager) if scripted else ModelManager(session_manager)
    )
    return Services(
        config=config,
        store=store,
        backend=backend,
        worktrees=worktrees,
        session_manager=session_manager,
        manager=manager,
        scripted=scripted,
    )


def register_repositories(
    session_manager: SessionManager, paths: Sequence[str | Path]
) -> list[str]:
    """Register repositories named on the command line. Returns user-visible notes."""
    notes: list[str] = []
    for path in paths:
        try:
            repo = session_manager.register_repository(path)
        except SessionManagerError as exc:
            notes.append(f"Could not register {path}: {exc}")
        else:
            notes.append(f"Registered repository {repo.name} ({repo.root_path}).")
    return notes


def build_app(register: Sequence[str | Path] = (), services: Services | None = None) -> SwitchboardApp:
    """Build the Textual app with its services already wired."""
    services = services or build_services()
    notes = register_repositories(services.session_manager, register)
    if services.scripted:
        notes.insert(0, "Scripted backend (SB_BACKEND=scripted): no model is called.")
    known = services.session_manager.list_repositories()
    if not known:
        notes.append("No repository is registered yet. Start with: sb --register /path/to/repo")
    app = SwitchboardApp(services.session_manager, services.manager, startup_notes=notes)
    app.services = services  # type: ignore[attr-defined]
    return app


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    `sb` and `sb claude` both open the interface: the bare form is what anyone types
    by reflex, and the named one is what a shell alias or a future Homebrew formula can
    point at without ambiguity. The remaining commands answer questions about the
    installation without starting it, which is what makes them worth having at all.
    """
    parser = argparse.ArgumentParser(
        prog="sb",
        description="A one-window control plane for multiple independent Claude sessions.",
    )
    parser.add_argument(
        "--register",
        metavar="PATH",
        action="append",
        default=[],
        help="Register a Git repository at startup (may be repeated).",
    )
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        default=None,
        help="Write application logs to this file instead of discarding them.",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("claude", help="Open the interface (the default).")
    commands.add_parser("workflows", help="List the workflows this installation can route to.")
    commands.add_parser("config", help="Print the effective configuration and its paths.")
    return parser.parse_args(argv)


def list_workflows() -> int:
    """Print every workflow, so routing can be inspected without opening the app."""
    services = build_services()
    try:
        problems = services.session_manager.reload_workflows()
        for name in workflow_names():
            definition = get_workflow(name)
            kind = "composite" if definition.is_composite else definition.default_role.value
            description = " ".join(definition.description.split())
            print(f"{name:<26} {kind:<16} {description}")
        for problem in problems:
            print(f"skipped: {problem}")
    finally:
        services.close()
    return 0


def show_config() -> int:
    """Print the effective configuration, including where everything is read from.

    `claude.env` is redacted: it is the one place in this file a token could be sitting,
    and the point of the command is to see where CSM is reading state from.
    """
    config = load_config()
    print(f"config file      {config_path()}")
    print(f"data directory   {home_dir()}")
    print(f"database         {database_path()}")
    print(f"worktree root    {worktree_root()}")
    print(f"user workflows   {user_workflows_dir()}")
    print()
    shown = config.model_dump(mode="json")
    shown["claude"]["env"] = {key: "<hidden>" for key in config.claude.env}
    print(json.dumps(shown, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.log_file:
        logging.basicConfig(filename=args.log_file, level=logging.INFO)
    else:
        logging.getLogger("switchboard").addHandler(logging.NullHandler())
    if args.command == "workflows":
        return list_workflows()
    if args.command == "config":
        return show_config()
    services = build_services()
    app = build_app(register=args.register, services=services)
    try:
        app.run()
    finally:
        services.close()
    return 0
