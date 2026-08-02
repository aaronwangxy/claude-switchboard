"""A focused, substrate-level tmux controller.

The controller knows tmux commands and metadata. Domain orchestration does not. One tmux
session represents one runtime generation; the session remains independently attachable and
discoverable if the Python process that created it exits.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from uuid import UUID, uuid4

from switchboard.domain.enums import RuntimeOwner


class TmuxError(RuntimeError):
    """A tmux operation failed or a runtime could not safely be controlled."""


class TmuxRuntimeStatus(str, Enum):
    ABSENT = "absent"
    ALIVE = "alive"
    EXITED = "exited"
    STALE = "stale"


@dataclass(frozen=True)
class RuntimeBinding:
    runtime_id: UUID
    generation: int
    launch_fingerprint: str


@dataclass(frozen=True)
class TmuxTarget:
    session_name: str
    pane_id: str
    pane_pid: int

    def as_substrate(self) -> dict[str, str]:
        return {
            "kind": "tmux",
            "session_name": self.session_name,
            "pane_id": self.pane_id,
            "pane_pid": str(self.pane_pid),
        }

    @classmethod
    def from_substrate(cls, value: Mapping[str, str]) -> TmuxTarget:
        try:
            if value["kind"] != "tmux":
                raise ValueError("not a tmux substrate")
            return cls(
                session_name=value["session_name"],
                pane_id=value["pane_id"],
                pane_pid=int(value["pane_pid"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TmuxError("The durable tmux target identity is incomplete or invalid.") from exc


@dataclass(frozen=True)
class TmuxObservation:
    status: TmuxRuntimeStatus
    target: TmuxTarget | None = None
    owner: RuntimeOwner | None = None
    attached_clients: int = 0
    exit_status: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class TmuxView:
    """Nonblocking ways for a caller to enter the existing runtime."""

    socket_path: Path
    external_argv: tuple[str, ...]
    nested_argv: tuple[str, ...]

    def argv(self, *, tmux_environment: str | None = None) -> tuple[str, ...]:
        current = os.getenv("TMUX") if tmux_environment is None else tmux_environment
        if not current:
            return self.external_argv
        current_socket = Path(current.split(",", 1)[0])
        if current_socket == self.socket_path:
            return self.nested_argv
        # Running the board inside tmux is ordinary, and it makes entry from this terminal
        # impossible -- so the refusal has to carry the command that does work, not just
        # the fact that this one does not.
        raise TmuxError(
            "The current terminal belongs to a different tmux server, so this session "
            "cannot be entered from here. Run this in another terminal:  "
            + shlex.join(self.external_argv)
        )


class TmuxController:
    """Own and inspect runtime sessions through argv-only tmux invocations."""

    _RUNTIME_ID = "@switchboard_runtime_id"
    _GENERATION = "@switchboard_generation"
    _FINGERPRINT = "@switchboard_launch_fingerprint"
    _OWNER = "@switchboard_owner"

    def __init__(self, socket_path: Path, executable: str = "tmux") -> None:
        self.socket_path = socket_path
        self.executable = executable

    def create(
        self,
        binding: RuntimeBinding,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> TmuxTarget:
        if not command:
            raise TmuxError("A runtime command cannot be empty.")
        self._ensure_server()
        session_name = self.session_name(binding.runtime_id)
        result = self._run(
            [
                "new-session",
                "-d",
                "-s",
                session_name,
                "-c",
                str(cwd),
                "-P",
                "-F",
                "#{pane_id}\t#{pane_pid}",
                *self._environment_args(env),
                *command,
            ],
            check=False,
        )
        if result.returncode != 0:
            existing = self.observe(binding, None)
            if existing.status is TmuxRuntimeStatus.ALIVE:
                raise TmuxError("The exact runtime already exists; adopt it instead of creating.")
            raise TmuxError(self._error(result, "Could not create tmux runtime."))
        try:
            pane_id, pane_pid = result.stdout.strip().split("\t", 1)
            target = TmuxTarget(session_name, pane_id, int(pane_pid))
        except (ValueError, TypeError) as exc:
            raise TmuxError("tmux did not return a usable pane identity.") from exc

        self._set_metadata(target, binding, RuntimeOwner.MANAGER)
        return target

    def observe(
        self, binding: RuntimeBinding, expected: TmuxTarget | None
    ) -> TmuxObservation:
        session_name = expected.session_name if expected else self.session_name(binding.runtime_id)
        present = self._run(["has-session", "-t", session_name], check=False)
        if present.returncode != 0:
            return TmuxObservation(TmuxRuntimeStatus.ABSENT, detail="tmux session is absent")
        result = self._run(
            [
                "display-message",
                "-p",
                "-t",
                session_name,
                "#{pane_id}\t#{pane_pid}\t#{pane_dead}\t#{pane_dead_status}"
                "\t#{session_attached}",
            ],
            check=False,
        )
        if result.returncode != 0:
            return TmuxObservation(TmuxRuntimeStatus.ABSENT, detail="tmux session is absent")
        try:
            pane_id, pane_pid, dead, dead_status, attached = result.stdout.strip().split("\t")
            actual = TmuxTarget(session_name, pane_id, int(pane_pid))
            attached_clients = int(attached)
        except (ValueError, TypeError) as exc:
            raise TmuxError("tmux returned malformed runtime state.") from exc

        metadata = self._metadata(actual)
        if not self._matches(metadata, binding) or (expected is not None and actual != expected):
            return TmuxObservation(
                TmuxRuntimeStatus.STALE,
                target=actual,
                attached_clients=attached_clients,
                detail="tmux target identity, generation, or fingerprint does not match",
            )
        try:
            owner = RuntimeOwner(metadata[self._OWNER])
        except (KeyError, ValueError):
            return TmuxObservation(
                TmuxRuntimeStatus.STALE,
                target=actual,
                attached_clients=attached_clients,
                detail="tmux ownership metadata is missing or invalid",
            )
        if dead == "1":
            return TmuxObservation(
                TmuxRuntimeStatus.EXITED,
                target=actual,
                owner=owner,
                attached_clients=attached_clients,
                exit_status=int(dead_status) if dead_status else None,
            )
        return TmuxObservation(
            TmuxRuntimeStatus.ALIVE,
            target=actual,
            owner=owner,
            attached_clients=attached_clients,
        )

    def set_owner(
        self, binding: RuntimeBinding, target: TmuxTarget, owner: RuntimeOwner
    ) -> None:
        with self._runtime_lock(binding):
            self._require_exact(binding, target, allow_exited=False)
            self._run(
                ["set-option", "-q", "-t", target.session_name, self._OWNER, owner.value]
            )

    def send_literal(self, binding: RuntimeBinding, target: TmuxTarget, text: str) -> None:
        with self._runtime_lock(binding):
            observation = self._require_exact(binding, target, allow_exited=False)
            if observation.owner is not RuntimeOwner.MANAGER:
                raise TmuxError(
                    "Runtime input is human-controlled; programmatic input is refused."
                )
            if observation.attached_clients:
                raise TmuxError(
                    "A tmux client is viewing this runtime; programmatic input is refused."
                )
            buffer_name = f"switchboard-{uuid4().hex}"
            self._run(
                ["load-buffer", "-b", buffer_name, "-"], input_bytes=text.encode("utf-8")
            )
            try:
                self._run(
                    ["paste-buffer", "-d", "-p", "-b", buffer_name, "-t", target.pane_id]
                )
            finally:
                # `-d` deletes on success; the explicit best-effort cleanup covers a pane
                # disappearing between load and paste so prompt contents do not linger.
                self._run(["delete-buffer", "-b", buffer_name], check=False)
            self._run(["send-keys", "-t", target.pane_id, "Enter"])

    def capture(self, binding: RuntimeBinding, target: TmuxTarget) -> str:
        """The visible pane text of an exact live runtime.

        Read-only, and used only as a guard: nothing decides orchestration state from
        screen scraping. Hooks remain the source of truth for what a session is doing.
        """
        self._require_exact(binding, target, allow_exited=False)
        result = self._run(["capture-pane", "-p", "-t", target.pane_id], check=False)
        return result.stdout if result.returncode == 0 else ""

    def answer_startup_dialog(self, binding: RuntimeBinding, target: TmuxTarget) -> None:
        """Choose the first option of a pre-session dialog, then confirm it.

        Deliberately not `send_literal`: that pastes into Claude's composer, which does
        not exist yet while a startup dialog is up.
        """
        with self._runtime_lock(binding):
            observation = self._require_exact(binding, target, allow_exited=False)
            if observation.owner is not RuntimeOwner.MANAGER:
                raise TmuxError("Runtime is human-controlled; programmatic input is refused.")
            if observation.attached_clients:
                raise TmuxError("A tmux client is viewing this runtime; input is refused.")
            self._run(["send-keys", "-t", target.pane_id, "1"])
            self._run(["send-keys", "-t", target.pane_id, "Enter"])

    def interrupt(self, binding: RuntimeBinding, target: TmuxTarget) -> None:
        """Send the terminal's normal Ctrl-C interrupt key to an owned live pane."""
        with self._runtime_lock(binding):
            observation = self._require_exact(binding, target, allow_exited=False)
            if observation.owner is not RuntimeOwner.MANAGER:
                raise TmuxError("Runtime is human-controlled; programmatic interrupt is refused.")
            if observation.attached_clients:
                raise TmuxError("A tmux client is viewing this runtime; interrupt is refused.")
            self._run(["send-keys", "-t", target.pane_id, "C-c"])

    def terminate(self, binding: RuntimeBinding, target: TmuxTarget) -> None:
        """Terminate only the exact generation-bound tmux session."""
        with self._runtime_lock(binding):
            self._require_exact(binding, target, allow_exited=True)
            self._run(["kill-session", "-t", target.session_name])

    def view(self, binding: RuntimeBinding, target: TmuxTarget) -> TmuxView:
        self._require_exact(binding, target, allow_exited=False)
        base = (self.executable, "-S", str(self.socket_path))
        return TmuxView(
            socket_path=self.socket_path,
            external_argv=(*base, "attach-session", "-t", target.session_name),
            nested_argv=(*base, "switch-client", "-t", target.session_name),
        )

    def _require_exact(
        self, binding: RuntimeBinding, target: TmuxTarget, *, allow_exited: bool
    ) -> TmuxObservation:
        observation = self.observe(binding, target)
        allowed = {TmuxRuntimeStatus.ALIVE}
        if allow_exited:
            allowed.add(TmuxRuntimeStatus.EXITED)
        if observation.status not in allowed:
            raise TmuxError(f"Runtime is {observation.status.value}: {observation.detail}")
        return observation

    def _set_metadata(
        self, target: TmuxTarget, binding: RuntimeBinding, owner: RuntimeOwner
    ) -> None:
        values = {
            self._RUNTIME_ID: str(binding.runtime_id),
            self._GENERATION: str(binding.generation),
            self._FINGERPRINT: binding.launch_fingerprint,
            self._OWNER: owner.value,
        }
        for key, value in values.items():
            self._run(["set-option", "-q", "-t", target.session_name, key, value])

    def _ensure_server(self) -> None:
        """Start a persistent dedicated server and set exit retention before launch."""
        bootstrap = f"switchboard-bootstrap-{uuid4().hex}"
        created = self._run(["new-session", "-d", "-s", bootstrap], check=False)
        if created.returncode == 0:
            self._run(["set-option", "-g", "exit-empty", "off"])
        # Window defaults apply to the runtime pane from its first instruction, so even
        # an immediately exiting child remains observable as EXITED.
        self._run(["set-option", "-g", "-w", "remain-on-exit", "on"])
        if created.returncode == 0:
            self._run(["kill-session", "-t", bootstrap])

    @contextmanager
    def _runtime_lock(self, binding: RuntimeBinding):
        """Serialize ownership and input transactions across controller processes."""
        channel = f"switchboard-lock-{binding.runtime_id.hex}-{binding.generation}"
        self._run(["wait-for", "-L", channel])
        try:
            yield
        finally:
            self._run(["wait-for", "-U", channel], check=False)

    def _metadata(self, target: TmuxTarget) -> dict[str, str]:
        values: dict[str, str] = {}
        for key in (self._RUNTIME_ID, self._GENERATION, self._FINGERPRINT, self._OWNER):
            result = self._run(
                ["show-options", "-qv", "-t", target.session_name, key], check=False
            )
            if result.returncode == 0 and result.stdout.strip():
                values[key] = result.stdout.strip()
        return values

    def _matches(self, metadata: Mapping[str, str], binding: RuntimeBinding) -> bool:
        return (
            metadata.get(self._RUNTIME_ID) == str(binding.runtime_id)
            and metadata.get(self._GENERATION) == str(binding.generation)
            and metadata.get(self._FINGERPRINT) == binding.launch_fingerprint
        )

    @staticmethod
    def session_name(runtime_id: UUID) -> str:
        return f"switchboard-{runtime_id.hex}"

    @staticmethod
    def _environment_args(env: Mapping[str, str] | None) -> list[str]:
        args: list[str] = []
        for key, value in sorted((env or {}).items()):
            if not key or "=" in key or "\x00" in key or "\x00" in value:
                raise TmuxError(f"Invalid environment entry {key!r}.")
            args.extend(["-e", f"{key}={value}"])
        return args

    def _run(
        self,
        args: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [self.executable, "-S", str(self.socket_path), *args],
            input=input_bytes,
            capture_output=True,
            text=False,
            check=False,
        )
        decoded = subprocess.CompletedProcess(
            result.args,
            result.returncode,
            stdout=result.stdout.decode("utf-8", errors="replace"),
            stderr=result.stderr.decode("utf-8", errors="replace"),
        )
        if check and decoded.returncode != 0:
            raise TmuxError(self._error(decoded, f"tmux {args[0]} failed."))
        return decoded

    @staticmethod
    def _error(result: subprocess.CompletedProcess[str], fallback: str) -> str:
        return result.stderr.strip() or result.stdout.strip() or fallback
