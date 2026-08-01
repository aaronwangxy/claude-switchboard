"""Experimental native Claude Code adapter over the tmux runtime substrate.

This is intentionally not a WorkerBackend. It proves launch, hooks, provenance, result
capture, interruption, entry, and adoption without changing production worker routing.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from switchboard.agents.runtime import ClaudeRuntimeError, claude_cli_path
from switchboard.config import Config
from switchboard.domain.contracts import extract_json_block
from switchboard.domain.enums import (
    NativeTurnOrigin,
    NativeTurnStatus,
    RuntimeOwner,
    RuntimeProcessState,
)
from switchboard.domain.models import NativeTurn, now
from switchboard.runtime.hook_bridge import acknowledge_turn, managed_prompt, prompt_digest
from switchboard.runtime.supervisor import SupervisedRuntime, TmuxRuntimeSupervisor
from switchboard.runtime.tmux import TmuxError, TmuxView
from switchboard.storage.store import Store

HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "Notification",
    "Stop",
    "StopFailure",
    "InstructionsLoaded",
    "SessionEnd",
)


@dataclass(frozen=True)
class NativeClaudeLaunch:
    runtime: SupervisedRuntime
    expected_session_id: str
    executable: Path
    settings_overlay: Path
    argv: tuple[str, ...]


class NativeClaudePrototype:
    def __init__(
        self,
        store: Store,
        supervisor: TmuxRuntimeSupervisor,
        config: Config,
        state_dir: Path,
        *,
        python_executable: str | None = None,
    ) -> None:
        self.store = store
        self.supervisor = supervisor
        self.config = config
        self.state_dir = state_dir
        self.python_executable = python_executable or sys.executable

    def launch(self, runtime_id: UUID, *, cwd: Path) -> NativeClaudeLaunch:
        executable = self._executable()
        runtime = self.store.get_runtime(runtime_id)
        if runtime is None:
            raise TmuxError("Runtime does not exist.")
        expected_fingerprint = self.launch_fingerprint(cwd=cwd, executable=executable)
        if runtime.launch_fingerprint != expected_fingerprint:
            raise TmuxError(
                "Native Claude launch fingerprint does not match the durable runtime."
            )
        # Re-running launch during recovery must describe/adopt the same Claude session,
        # not allocate a replacement identity before the supervisor can observe tmux.
        session_id = runtime.claude_session_id or str(uuid4())
        runtime.claude_session_id = session_id
        runtime.updated_at = now()
        self.store.save_runtime(runtime)
        settings = self._write_settings(runtime_id)
        argv = (
            str(executable),
            "--session-id",
            session_id,
            "--settings",
            str(settings),
            "--setting-sources",
            ",".join(self.config.setting_sources),
        )
        launched = self.supervisor.launch(
            runtime_id,
            argv,
            cwd=cwd,
            env=self.config.claude.env,
        )
        return NativeClaudeLaunch(launched, session_id, executable, settings, argv)

    def launch_fingerprint(self, *, cwd: Path, executable: Path | None = None) -> str:
        """Hash every stable launch input that defines a reusable native process."""
        payload = {
            "adapter": "native-claude-prototype-v1",
            "cwd": str(cwd.resolve()),
            "env": self.config.claude.env,
            "executable": str((executable or self._executable()).resolve()),
            "hook_database": str(self.store.path.resolve()),
            "hook_events": HOOK_EVENTS,
            "hook_python": self.python_executable,
            "state_dir": str(self.state_dir.resolve()),
            "setting_sources": self.config.setting_sources,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def adopt(self, runtime_id: UUID, *, cwd: Path) -> SupervisedRuntime:
        runtime = self.store.get_runtime(runtime_id)
        if runtime is None:
            raise TmuxError("Runtime does not exist.")
        if runtime.launch_fingerprint != self.launch_fingerprint(cwd=cwd):
            raise TmuxError(
                "Native Claude launch fingerprint does not match this controller configuration."
            )
        return self.supervisor.observe(runtime_id)

    def send_managed(self, runtime_id: UUID, prompt: str) -> NativeTurn:
        runtime = self.store.get_runtime(runtime_id)
        if runtime is None:
            raise TmuxError("Runtime does not exist.")
        if runtime.process_state is not RuntimeProcessState.READY:
            raise TmuxError(f"Native Claude runtime is {runtime.process_state.value}, not ready.")
        if self.store.open_native_turn(runtime_id) is not None:
            raise TmuxError("Native Claude already has an active turn; input is refused.")
        turn = NativeTurn(
            runtime_id=runtime_id,
            origin=NativeTurnOrigin.MANAGED,
            correlation_token=secrets.token_urlsafe(32),
            prompt_sha256=prompt_digest(prompt),
        )
        self.store.save_native_turn(turn)
        try:
            self.supervisor.send(runtime_id, managed_prompt(turn, prompt))
        except Exception as exc:
            turn.status = NativeTurnStatus.FAILED
            turn.error = f"Input injection failed: {exc}"
            turn.updated_at = now()
            self.store.save_native_turn(turn)
            raise
        return turn

    def completed(self, turn_id: UUID) -> NativeTurn | None:
        turn = self.store.get_native_turn(turn_id)
        if turn is None:
            return None
        return (
            turn
            if turn.status
            in (
                NativeTurnStatus.COMPLETED,
                NativeTurnStatus.FAILED,
                NativeTurnStatus.INTERRUPTED,
            )
            else None
        )

    def acknowledge(self, runtime_id: UUID, turn_id: UUID) -> NativeTurn:
        return acknowledge_turn(self.store, runtime_id, turn_id)

    def artifact(self, turn_id: UUID) -> dict | None:
        turn = self.store.get_native_turn(turn_id)
        return extract_json_block(turn.final_output) if turn else None

    def interrupt(self, runtime_id: UUID, turn_id: UUID) -> NativeTurn:
        turn = self.store.get_native_turn(turn_id)
        if turn is None or turn.runtime_id != runtime_id:
            raise TmuxError("No such native turn for this runtime.")
        if turn.status not in (
            NativeTurnStatus.ACTIVE,
            NativeTurnStatus.WAITING_PERMISSION,
        ):
            raise TmuxError(f"Native turn is {turn.status.value}, not interruptible.")
        self.supervisor.interrupt(runtime_id)
        # Delivery of Ctrl-C is not semantic confirmation that Claude stopped. Keep the
        # input lane closed until a supported event or explicit human recovery resolves it.
        turn.status = NativeTurnStatus.INTERRUPT_REQUESTED
        turn.error = "Switchboard sent native Ctrl-C; interruption is not yet confirmed."
        turn.updated_at = now()
        self.store.save_native_turn(turn)
        runtime = self.store.get_runtime(runtime_id)
        if runtime is not None:
            runtime.process_state = RuntimeProcessState.TURN_ACTIVE
            runtime.updated_at = now()
            self.store.save_runtime(runtime)
        return turn

    def claim_human(self, runtime_id: UUID) -> TmuxView:
        self.supervisor.set_owner(runtime_id, RuntimeOwner.HUMAN)
        return self.supervisor.view(runtime_id)

    def release_human(self, runtime_id: UUID, *, composer_cleared: bool) -> None:
        if not composer_cleared:
            raise TmuxError(
                "Human handback requires explicit confirmation that Claude's composer is empty."
            )
        self.supervisor.set_owner(runtime_id, RuntimeOwner.MANAGER)

    def _write_settings(self, runtime_id: UUID) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / f"native-{runtime_id}.settings.json"
        command = shlex.join(
            [
                self.python_executable,
                "-m",
                "switchboard.runtime.hook_bridge",
                "--database",
                str(self.store.path),
                "--runtime-id",
                str(runtime_id),
            ]
        )
        hook = {"hooks": [{"type": "command", "command": command, "timeout": 10}]}
        path.write_text(json.dumps({"hooks": {event: [hook] for event in HOOK_EVENTS}}, indent=2))
        path.chmod(0o600)
        return path

    def _executable(self) -> Path:
        configured = claude_cli_path(self.config.claude.executable)
        if configured is not None:
            return configured
        found = shutil.which("claude")
        if not found:
            raise ClaudeRuntimeError("Claude executable was not found on PATH.")
        return Path(found)
