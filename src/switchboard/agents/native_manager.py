"""Persistent native Claude manager using the worker-proven tmux/runtime substrate."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from uuid import UUID, uuid4

from switchboard.agents.attach import Attachment
from switchboard.agents.manager import APPROVE_RE, CONFIRM_RE, Manager
from switchboard.agents.manager_mcp import ManagerTools, serve_connection
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
MAX_MANAGER_TURNS = 80
FRESH_MANAGER_RE = re.compile(
    r"^\s*(?:start|give me|use|rotate to|create)?\s*(?:a\s+)?fresh manager\s*[.!]?\s*$",
    re.I,
)


class PersistentNativeManager(Manager):
    """One durable manager identity with a replaceable, generation-bound Claude process."""

    def __init__(self, sm: SessionManager, backend: NativeClaudeBackend, state_dir: Path) -> None:
        self.sm = sm
        self.backend = backend
        self.state_dir = state_dir
        self.workspace = state_dir / "manager-workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._assert_clean_workspace()
        self.manager_id = UUID(
            sm.store.get_or_create_preference(MANAGER_ID_KEY, str(uuid4()))
        )
        self._lock = asyncio.Lock()
        self._mcp_servers: dict[UUID, asyncio.AbstractServer] = {}

    @property
    def current_runtime(self) -> RuntimeInstance | None:
        return self.sm.store.current_runtime(self.manager_id)

    async def start_or_recover(self) -> RuntimeInstance:
        current = self.current_runtime
        if current is not None:
            await self._ensure_mcp_server(current)
            try:
                observed = self.backend.runtime.adopt(
                    current.id,
                    cwd=self.workspace,
                    model=self.sm.config.models.manager,
                    system_prompt_append=self._prompt(current),
                    extra_args=self._extra_args(current),
                )
            except TmuxError as exc:
                observed = self.backend.supervisor.observe(current.id)
                if observed.observation.status is TmuxRuntimeStatus.ALIVE:
                    raise TmuxError(
                        "The live Manager process does not match the current launch "
                        "configuration. Refusing to create a duplicate; finish or stop the "
                        "existing Manager session explicitly."
                    ) from exc
            else:
                if observed.observation.status is TmuxRuntimeStatus.ALIVE:
                    return observed.runtime
        return await self._new_generation(expected_current_id=current.id if current else None)

    async def handle(self, text: str) -> str:
        async with self._lock:
            if FRESH_MANAGER_RE.match(text):
                objective = self.sm.store.get_preference(MANAGER_OBJECTIVE_KEY, "") or ""
                replacement = await self._rotate_unlocked({"current_user_objective": objective})
                return f"Started fresh Manager generation {replacement.generation}."
            runtime = await self.start_or_recover()
            if len(self.sm.store.list_native_turns(runtime.id)) >= MAX_MANAGER_TURNS:
                objective = self.sm.store.get_preference(MANAGER_OBJECTIVE_KEY, "") or ""
                runtime = await self._rotate_unlocked(
                    {
                        "current_user_objective": objective,
                        "rotation_reason": "bounded manager turn limit reached",
                    }
                )
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
            return await self._rotate_unlocked(handoff)

    async def _rotate_unlocked(
        self, handoff: dict[str, object] | None = None
    ) -> RuntimeInstance:
        if handoff:
            bounded = {key: str(value)[:1000] for key, value in list(handoff.items())[:6]}
            encoded = json.dumps(bounded, separators=(",", ":"))[:MAX_HANDOFF_CHARS]
            self.sm.store.set_preference(MANAGER_HANDOFF_KEY, encoded)
        old = self.current_runtime
        if old is not None:
            old.owner = RuntimeOwner.HUMAN  # closes autonomous input before teardown
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
        try:
            argv = list(view.argv())
        except Exception:
            self.backend.runtime.release_human(runtime.id, composer_cleared=True)
            raise
        return Attachment(
            cwd=self.workspace,
            session_id=runtime.claude_session_id or "",
            argv=argv,
            note="Entered the same live native Manager Claude process.",
        )

    def release_human(self, *, composer_cleared: bool) -> None:
        runtime = self.current_runtime
        if runtime is None:
            raise TmuxError("Manager runtime is missing.")
        self.backend.runtime.release_human(runtime.id, composer_cleared=composer_cleared)
        turns = self.sm.store.list_native_turns(runtime.id)
        refreshed = self.sm.store.get_runtime(runtime.id)
        if (
            turns
            and refreshed is not None
            and refreshed.process_state is RuntimeProcessState.TURN_COMPLETE
            and self.backend.runtime.completed(turns[-1].id) is not None
        ):
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
        await self._ensure_mcp_server(runtime)
        pending_handoff = self.sm.store.get_preference(MANAGER_HANDOFF_KEY, "") or ""
        self.sm.store.set_preference(f"manager.handoff.{runtime.id}", pending_handoff)
        self.sm.store.set_preference(MANAGER_HANDOFF_KEY, "")
        runtime.launch_fingerprint = self.backend.runtime.launch_fingerprint(
            cwd=self.workspace,
            model=self.sm.config.models.manager,
            system_prompt_append=self._prompt(runtime),
            extra_args=self._extra_args(runtime),
        )
        self.sm.store.save_runtime(runtime)
        launched = self.backend.runtime.launch(
            runtime.id,
            cwd=self.workspace,
            model=self.sm.config.models.manager,
            system_prompt_append=self._prompt(runtime),
            extra_args=self._extra_args(runtime),
        )
        await self.backend._wait_ready(runtime.id)
        return launched.runtime.runtime

    def _prompt(self, runtime: RuntimeInstance) -> str:
        handoff = self.sm.store.get_preference(f"manager.handoff.{runtime.id}", "") or ""
        suffix = (
            "\n\nThis is bounded handoff from the previous manager generation. Re-read "
            "authoritative state through Switchboard tools before acting:\n" + handoff
            if handoff
            else ""
        )
        return compose_manager_prompt() + suffix

    def _mcp_path(self, runtime: RuntimeInstance) -> Path:
        return self.state_dir / "manager" / f"mcp-{runtime.id}.json"

    def _socket_path(self, runtime: RuntimeInstance) -> Path:
        # AF_UNIX paths are limited to roughly 100 bytes on macOS; SB_HOME test and user
        # paths can exceed that before the UUID is appended.
        root = Path("/private/tmp") if Path("/private/tmp").is_dir() else Path("/tmp")
        return root / f"sb-manager-{runtime.id}.sock"

    async def _ensure_mcp_server(self, runtime: RuntimeInstance) -> None:
        if runtime.id in self._mcp_servers:
            return
        path = self._socket_path(runtime)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.is_socket():
            path.unlink()
        tools = ManagerTools(self.sm, self.manager_id, runtime.id, runtime.generation)

        async def connected(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await serve_connection(tools, reader, writer)

        self._mcp_servers[runtime.id] = await asyncio.start_unix_server(connected, path)
        path.chmod(0o600)

    def _write_mcp_config(self, runtime: RuntimeInstance) -> None:
        path = self._mcp_path(runtime)
        path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "switchboard.agents.manager_mcp",
            "--socket",
            str(self._socket_path(runtime)),
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
