"""Application bootstrap: wire the services, then hand them to the UI.

Nothing here renders anything, and nothing in the UI constructs a service. Production workers
are persistent native Claude processes; `SB_BACKEND=scripted` swaps them for the deterministic
in-process backend and the model manager for the rule-based one for offline tests and demos.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from switchboard.agents.backend import WorkerBackend
from switchboard.agents.manager import DeterministicManager, Manager
from switchboard.config import (
    Config,
    config_path,
    database_path,
    home_dir,
    load_config,
    user_workflows_dir,
    worktree_root,
)
from switchboard.core.evidence import required_artifacts
from switchboard.core.session_manager import SessionManager, SessionManagerError
from switchboard.gitops.worktrees import WorktreeService
from switchboard.storage.store import Store
from switchboard.ui.screens import SwitchboardApp
from switchboard.workflows.registry import get_workflow, workflow_names
from switchboard.workflows.validate import validate_registry

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
        self.session_manager.shutdown()
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
        from switchboard.agents.native_backend import NativeClaudeBackend

        runtime_dir = home_dir() / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        backend = NativeClaudeBackend(store, config, runtime_dir)
    worktrees = WorktreeService(worktree_root(), config.worktree_bootstrap.files)
    session_manager = SessionManager(store, backend, config, worktrees)
    session_manager.reload_workflows()
    if scripted:
        manager: Manager = DeterministicManager(session_manager)
    else:
        from switchboard.agents.native_manager import PersistentNativeManager

        assert isinstance(backend, NativeClaudeBackend)
        manager = PersistentNativeManager(session_manager, backend, home_dir())
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


def build_app(
    register: Sequence[str | Path] = (), services: Services | None = None
) -> SwitchboardApp:
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
    workflows = commands.add_parser(
        "workflows", help="List the workflows this installation can route to."
    )
    workflows.add_argument(
        "action",
        nargs="?",
        choices=["list", "validate"],
        default="list",
        help="'validate' checks every workflow for authoring mistakes and exits non-zero.",
    )
    commands.add_parser("config", help="Print the effective configuration and its paths.")
    kill = commands.add_parser(
        "kill", help="Stop the board and every native session it launched."
    )
    kill.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Do not ask for confirmation.",
    )
    return parser.parse_args(argv)


def list_workflows() -> int:
    """Print every workflow, so routing can be inspected without opening the app."""
    services = build_services()
    try:
        problems = services.session_manager.reload_workflows()
        for name in workflow_names():
            definition = get_workflow(name)
            kind = "composite" if definition.is_composite else definition.role.value
            description = " ".join(definition.description.split())
            done = ", ".join(sorted(a.value for a in required_artifacts(definition)))
            print(f"{name:<26} {kind:<14} {description}")
            if done:
                print(f"{'':<26} {'':<14} done when: {done}")
        for problem in problems:
            print(f"skipped: {problem}")
    finally:
        services.close()
    return 0


def validate_workflows() -> int:
    """Check every workflow before somebody's work depends on one being right."""
    services = build_services()
    try:
        services.session_manager.reload_workflows()
        problems = validate_registry()
        for problem in problems:
            print(problem)
        if problems:
            print(f"\n{len(problems)} problem(s).")
            return 1
        print(f"{len(workflow_names())} workflows, no problems.")
    finally:
        services.close()
    return 0


def show_config() -> int:
    """Print the effective configuration, including where everything is read from.

    `claude.env` is redacted: it is the one place in this file a token could be sitting,
    and the point of the command is to see where Switchboard is reading state from.
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


def board_processes() -> list[tuple[int, str]]:
    """Processes holding *this* data directory's database open.

    Scoped by the database rather than by process name on purpose: a board running under
    a throwaway `SB_HOME` and your own board are indistinguishable in `ps`, and stopping
    the wrong one is the mistake this command must not make.
    """
    lsof = shutil.which("lsof")
    database = database_path()
    if lsof is None or not database.exists():
        return []
    result = subprocess.run(
        [lsof, "-t", "--", str(database)], capture_output=True, text=True, check=False
    )
    asking = {os.getpid(), os.getppid()}
    found: list[tuple[int, str]] = []
    for pid_text in result.stdout.split():
        if not pid_text.isdigit() or int(pid_text) in asking:
            continue
        pid = int(pid_text)
        described = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False
        )
        found.append((pid, " ".join(described.stdout.split()) or "?"))
    return found


def runtime_processes() -> list[tuple[int, str]]:
    """Native Claude processes launched by *this* data directory.

    Killing the tmux server is not enough: a pane's Claude survives losing its terminal,
    so it has to be signalled by name. Every runtime is launched with a `--settings` file
    under this home's hooks directory, which is what makes it identifiably ours.
    """
    marker = str(home_dir() / "runtime" / "hooks")
    result = subprocess.run(
        ["ps", "-eo", "pid=,command="], capture_output=True, text=True, check=False
    )
    found: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        if not pid_text.isdigit() or marker not in command:
            continue
        found.append((int(pid_text), " ".join(command.split()[:2])))
    return found


def _stop(processes: Sequence[tuple[int, str]], label: str) -> None:
    """Ask each process to exit, then insist."""
    for pid, command in processes:
        with suppress(OSError):
            os.kill(pid, signal.SIGTERM)
            print(f"stopping {label} {pid} ({command})")
    for _ in range(30):
        if not any(_alive(pid) for pid, _ in processes):
            return
        time.sleep(0.1)
    for pid, _ in processes:
        if _alive(pid):
            with suppress(OSError):
                os.kill(pid, signal.SIGKILL)
                print(f"killed {label} {pid}")


def kill_everything(*, assume_yes: bool) -> int:
    """Stop the board and tear down the tmux server holding the manager and workers.

    The escape hatch for a board that cannot be quit from its own UI. It reads no
    orchestration state and writes none, so it still works when nothing else does.
    Worktrees, branches and the database are untouched.

    It is not a restart, though: a killed runtime is reconstructed as a *fresh* native
    session, so each worker loses the conversation it was holding, and a composite run
    whose step was mid-flight is paused for reconciliation rather than resent. Quitting
    the board leaves the sessions alive precisely to avoid that; this is for when you
    want them gone.

    Anything launched under a different `SB_HOME` belongs to a different data directory
    and is deliberately out of reach -- hence printing the socket this is acting on.
    """
    from switchboard.agents.native_backend import default_tmux_socket_path

    socket = default_tmux_socket_path(home_dir() / "runtime")
    tmux = shutil.which("tmux")
    sessions: list[str] = []
    if tmux and socket.exists():
        listed = subprocess.run(
            [tmux, "-S", str(socket), "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            check=False,
        )
        sessions = [name for name in listed.stdout.split() if name]
    boards = board_processes()
    runtimes = runtime_processes()

    print(f"data directory   {home_dir()}")
    print(f"tmux socket      {socket}")
    print(f"board processes  {len(boards)}")
    print(f"native sessions  {len(sessions)}")
    print(f"claude processes {len(runtimes)}")
    if not boards and not sessions and not runtimes:
        print("\nNothing to stop.")
        return 0
    if not assume_yes:
        answer = input("\nStop all of it? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Left alone.")
            return 1

    # The board first: it is what would notice the sessions disappearing and recreate
    # them. Then the tmux server, and then the Claude processes that outlive their panes.
    _stop(boards, "board")
    if sessions and tmux:
        subprocess.run(
            [tmux, "-S", str(socket), "kill-server"], capture_output=True, check=False
        )
        print(f"killed {len(sessions)} native session(s)")
    _stop(runtime_processes(), "claude")
    print("\nDone. Run `sb` to bring the workers back.")
    return 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.log_file:
        logging.basicConfig(filename=args.log_file, level=logging.INFO)
    else:
        logging.getLogger("switchboard").addHandler(logging.NullHandler())
    if args.command == "workflows":
        return validate_workflows() if args.action == "validate" else list_workflows()
    if args.command == "config":
        return show_config()
    if args.command == "kill":
        return kill_everything(assume_yes=args.yes)
    services = build_services()
    app = build_app(register=args.register, services=services)
    try:
        app.run()
    finally:
        services.close()
    return 0
