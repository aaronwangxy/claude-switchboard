"""Persistent native Claude manager using the worker-proven tmux/runtime substrate."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

from switchboard.agents.attach import Attachment
from switchboard.agents.manager import APPROVE_RE, CONFIRM_RE, Manager
from switchboard.agents.native_backend import NativeClaudeBackend
from switchboard.agents.prompts import compose_manager_prompt
from switchboard.core.session_manager import SessionManager
from switchboard.domain.enums import (
    NativeTurnStatus,
    RuntimeAgentKind,
    RuntimeOwner,
    RuntimeProcessState,
)
from switchboard.domain.models import RuntimeInstance, now
from switchboard.runtime.tmux import TmuxError, TmuxRuntimeStatus

MANAGER_ID_KEY = "manager.identity"
MANAGER_HANDOFF_KEY = "manager.handoff"
MANAGER_OBJECTIVE_KEY = "manager.current_objective"
MAX_HANDOFF_CHARS = 4000


class PersistentNativeManager(Manager):
    """One durable manager identity with a replaceable, generation-bound Claude process."""

    def __init__(self, sm: SessionManager, backend: NativeClaudeBackend, state_dir: Path) -> None:
        self.sm = sm
        self.backend = backend
        self.state_dir = state_dir
        self.runtime_state_dir = backend.controller.socket_path.parent
        self.workspace = state_dir / "manager-workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._assert_clean_workspace()
        value = sm.store.get_preference(MANAGER_ID_KEY)
        self.manager_id = UUID(value) if value else uuid4()
        if value is None:
            sm.store.set_preference(MANAGER_ID_KEY, str(self.manager_id))
        self._lock = asyncio.Lock()

    @property
    def current_runtime(self) -> RuntimeInstance | None:
        return self.sm.store.current_runtime(self.manager_id)

    async def start_or_recover(self) -> RuntimeInstance:
        current = self.current_runtime
        if current is not None:
            try:
                observed = self.backend.runtime.adopt(
                    current.id,
                    cwd=self.workspace,
                    model=self.sm.config.models.manager,
                    system_prompt_append=self._prompt(),
                    extra_args=self._extra_args(current),
                )
            except TmuxError:
                pass
            else:
                if observed.observation.status is TmuxRuntimeStatus.ALIVE:
                    return observed.runtime
        return await self._new_generation(expected_current_id=current.id if current else None)

    async def handle(self, text: str) -> str:
        async with self._lock:
            runtime = await self.start_or_recover()
            if runtime.owner is RuntimeOwner.HUMAN:
                return "Manager is currently owned by the human session; automated input is paused."
            self.sm.store.set_preference(MANAGER_OBJECTIVE_KEY, text[:1000])
            turn = self.backend.runtime.send_managed(runtime.id, text)
            self.sm.store.set_preference(
                "manager.confirmed_turn", str(turn.id) if CONFIRM_RE.search(text) else ""
            )
            self.sm.store.set_preference(
                "manager.approval_turn", str(turn.id) if APPROVE_RE.search(text) else ""
            )
            deadline = asyncio.get_running_loop().time() + 180
            while asyncio.get_running_loop().time() < deadline:
                terminal = self.backend.runtime.completed(turn.id)
                if terminal is not None:
                    self.backend.runtime.acknowledge(runtime.id, turn.id)
                    if terminal.status is NativeTurnStatus.COMPLETED:
                        return terminal.final_output.strip() or self.sm.status_summary()
                    return f"Manager turn failed: {terminal.error or terminal.final_output}"
                await asyncio.sleep(0.05)
            return "Manager is still working in its native session."

    async def rotate(self, handoff: dict[str, object] | None = None) -> RuntimeInstance:
        """Revoke the old generation before starting a fresh native Claude session."""
        async with self._lock:
            if handoff:
                bounded = {key: str(value)[:1000] for key, value in list(handoff.items())[:6]}
                encoded = json.dumps(bounded, separators=(",", ":"))[:MAX_HANDOFF_CHARS]
                self.sm.store.set_preference(MANAGER_HANDOFF_KEY, encoded)
            old = self.current_runtime
            if old is not None:
                old.owner = RuntimeOwner.HUMAN  # revokes MCP authority before process teardown
                old.process_state = RuntimeProcessState.EXITED
                old.updated_at = now()
                self.sm.store.save_runtime(old)
                if old.substrate:
                    try:
                        self.backend.supervisor.terminate(old.id)
                    except TmuxError:
                        pass
            return await self._new_generation(force=True)

    async def enter(self) -> Attachment:
        runtime = await self.start_or_recover()
        view = self.backend.runtime.claim_human(runtime.id)
        return Attachment(
            cwd=self.workspace,
            session_id=runtime.claude_session_id or "",
            argv=list(view.external_argv),
            note="Entered the same live native Manager Claude process.",
        )

    def release_human(self, *, composer_cleared: bool) -> None:
        runtime = self.current_runtime
        if runtime is None:
            raise TmuxError("Manager runtime is missing.")
        self.backend.runtime.release_human(runtime.id, composer_cleared=composer_cleared)
        turns = self.sm.store.list_native_turns(runtime.id)
        if turns and self.backend.runtime.completed(turns[-1].id) is not None:
            self.backend.runtime.acknowledge(runtime.id, turns[-1].id)

    def status(self) -> dict[str, object]:
        runtime = self.current_runtime
        return {
            "manager_id": str(self.manager_id),
            "runtime_id": str(runtime.id) if runtime else None,
            "generation": runtime.generation if runtime else None,
            "session_id": runtime.claude_session_id if runtime else None,
            "state": runtime.process_state.value if runtime else "absent",
            "owner": runtime.owner.value if runtime else None,
            "workspace": str(self.workspace),
        }

    async def _new_generation(
        self, *, expected_current_id: UUID | None = None, force: bool = False
    ) -> RuntimeInstance:
        # BEGIN IMMEDIATE serializes competing board processes. The generation row is the
        # authority lease; a second controller observes it rather than minting a peer.
        with self.sm.store.transaction():
            previous = self.current_runtime
            if not force and previous is not None and previous.id != expected_current_id:
                runtime = previous
                created = False
            else:
                runtime = RuntimeInstance(
                    agent_id=self.manager_id,
                    agent_kind=RuntimeAgentKind.MANAGER,
                    generation=(previous.generation + 1) if previous else 1,
                    backend="native-claude",
                )
                self.sm.store.save_runtime(runtime)
                created = True
        if not created:
            return await self.start_or_recover()
        self._write_mcp_config(runtime)
        runtime.launch_fingerprint = self.backend.runtime.launch_fingerprint(
            cwd=self.workspace,
            model=self.sm.config.models.manager,
            system_prompt_append=self._prompt(),
            extra_args=self._extra_args(runtime),
        )
        self.sm.store.save_runtime(runtime)
        launched = self.backend.runtime.launch(
            runtime.id,
            cwd=self.workspace,
            model=self.sm.config.models.manager,
            system_prompt_append=self._prompt(),
            extra_args=self._extra_args(runtime),
        )
        await self.backend._wait_ready(runtime.id)
        return launched.runtime.runtime

    def _prompt(self) -> str:
        handoff = self.sm.store.get_preference(MANAGER_HANDOFF_KEY, "") or ""
        suffix = (
            "\n\nThis is bounded handoff from the previous manager generation. Re-read "
            "authoritative state through Switchboard tools before acting:\n" + handoff
            if handoff
            else ""
        )
        return compose_manager_prompt() + suffix

    def _mcp_path(self, runtime: RuntimeInstance) -> Path:
        return self.state_dir / "manager" / f"mcp-{runtime.id}.json"

    def _write_mcp_config(self, runtime: RuntimeInstance) -> None:
        path = self._mcp_path(runtime)
        path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "switchboard.agents.manager_mcp",
            "--database",
            str(self.sm.store.path),
            "--manager-id",
            str(self.manager_id),
            "--runtime-id",
            str(runtime.id),
            "--generation",
            str(runtime.generation),
            "--state-dir",
            str(self.runtime_state_dir),
        ]
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "switchboard": {"type": "stdio", "command": command[0], "args": command[1:]}
                    }
                },
                indent=2,
            )
        )
        path.chmod(0o600)

    def _extra_args(self, runtime: RuntimeInstance) -> tuple[str, ...]:
        return (
            "--mcp-config",
            str(self._mcp_path(runtime)),
            "--strict-mcp-config",
            "--tools",
            "",
            "--allowedTools",
            "mcp__switchboard__*",
            "--disallowedTools",
            "Bash,Edit,Write,Read,Glob,Grep,Task,WebFetch,WebSearch,NotebookEdit",
        )

    def _assert_clean_workspace(self) -> None:
        if any(self.workspace.iterdir()):
            unexpected = [
                p.name for p in self.workspace.iterdir() if p.name != ".switchboard-manager"
            ]
            if unexpected:
                raise RuntimeError(f"Manager workspace is not clean: {', '.join(unexpected)}")
        marker = self.workspace / ".switchboard-manager"
        marker.touch(exist_ok=True)
        for parent in (self.workspace, *self.workspace.parents):
            if (parent / ".git").exists():
                raise RuntimeError(
                    "Manager workspace is inside a Git repository; refusing unsafe launch."
                )
