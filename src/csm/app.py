"""Application bootstrap: wire the services, then hand them to the UI.

Nothing here renders anything, and nothing in the UI constructs a service. The backend is
chosen once, here: `CSM_BACKEND=scripted` swaps the Agent SDK for the deterministic
in-process backend and the model manager for the rule-based one, so the whole UI can be
demoed and tested offline.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from csm.agents.backend import WorkerBackend
from csm.agents.manager import DeterministicManager, Manager, ModelManager
from csm.config import Config, database_path, home_dir, load_config, worktree_root
from csm.core.session_manager import SessionManager, SessionManagerError
from csm.gitops.worktrees import WorktreeService
from csm.storage.store import Store
from csm.ui.screens import CsmApp

log = logging.getLogger(__name__)

BACKEND_ENV = "CSM_BACKEND"


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
        from csm.agents.scripted_backend import ScriptedWorkerBackend

        backend = ScriptedWorkerBackend()
    else:
        from csm.agents.sdk_backend import SdkWorkerBackend

        backend = SdkWorkerBackend()
    worktrees = WorktreeService(worktree_root(), config.worktree_bootstrap.files)
    session_manager = SessionManager(store, backend, config, worktrees)
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


def build_app(register: Sequence[str | Path] = (), services: Services | None = None) -> CsmApp:
    """Build the Textual app with its services already wired."""
    services = services or build_services()
    notes = register_repositories(services.session_manager, register)
    if services.scripted:
        notes.insert(0, "Scripted backend (CSM_BACKEND=scripted): no model is called.")
    known = services.session_manager.list_repositories()
    if not known:
        notes.append("No repository is registered yet. Start with: csm --register /path/to/repo")
    app = CsmApp(services.session_manager, services.manager, startup_notes=notes)
    app.services = services  # type: ignore[attr-defined]
    return app


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="csm",
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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.log_file:
        logging.basicConfig(filename=args.log_file, level=logging.INFO)
    else:
        logging.getLogger("csm").addHandler(logging.NullHandler())
    services = build_services()
    app = build_app(register=args.register, services=services)
    try:
        app.run()
    finally:
        services.close()
    return 0
