"""Agent SDK backend: one independent `ClaudeSDKClient` per active worker."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import UUID

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from csm.agents.backend import BackendHealth, EventType, WorkerEvent, WorkerHandle, WorkerSpec

log = logging.getLogger(__name__)

#: Tools a read-only worker may use. Deliberately excludes every file-mutating tool.
READ_ONLY_TOOLS = ["Read", "Grep", "Glob", "Bash", "WebFetch", "WebSearch", "TodoWrite", "Task"]

#: Never available to any worker: workers must not touch the manager's registry.
ALWAYS_DISALLOWED: list[str] = []

WRITE_TOOLS = ["Edit", "Write", "NotebookEdit", "MultiEdit"]

#: Signals in a worker's final message that mean it is waiting on the user.
BLOCKED_MARKERS = ("[NEEDS INPUT]", "[NEEDS DECISION]")


@dataclass
class _Session:
    spec: WorkerSpec
    inbox: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    outbox: asyncio.Queue[WorkerEvent | None] = field(default_factory=asyncio.Queue)
    client: ClaudeSDKClient | None = None
    task: asyncio.Task | None = None
    session_id: str | None = None
    alive: bool = True
    detail: str = ""
    helpers: int = 0


class SdkWorkerBackend:
    """Runs each worker as its own long-lived SDK session in its own working directory.

    Workers get the normal Claude Code coding-agent preset plus this product's policy
    text. They are never given the manager's in-process tools or registry state.
    """

    def __init__(self) -> None:
        self._sessions: dict[UUID, _Session] = {}

    # ------------------------------------------------------------------ public

    async def start(self, spec: WorkerSpec) -> WorkerHandle:
        return await self._launch(spec)

    async def resume(self, spec: WorkerSpec) -> WorkerHandle:
        return await self._launch(spec)

    async def send(self, worker_id: UUID, message: str) -> None:
        session = self._require(worker_id)
        await session.inbox.put(message)

    async def stream(self, worker_id: UUID) -> AsyncIterator[WorkerEvent]:
        session = self._require(worker_id)
        while True:
            event = await session.outbox.get()
            if event is None:
                return
            yield event

    async def interrupt(self, worker_id: UUID) -> None:
        session = self._require(worker_id)
        if session.client is not None:
            try:
                await session.client.interrupt()
            except Exception as exc:  # the SDK raises if no turn is in flight
                log.debug("interrupt on %s: %s", worker_id, exc)

    async def stop(self, worker_id: UUID) -> None:
        session = self._sessions.pop(worker_id, None)
        if session is None:
            return
        session.alive = False
        await session.inbox.put(None)
        if session.task is not None:
            session.task.cancel()
            try:
                await session.task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - shutdown is best effort
                pass
        await session.outbox.put(WorkerEvent(worker_id, "stopped", "Session stopped."))
        await session.outbox.put(None)

    async def health(self, worker_id: UUID) -> BackendHealth:
        session = self._sessions.get(worker_id)
        if session is None:
            return BackendHealth(alive=False, detail="No live session for this worker.")
        return BackendHealth(alive=session.alive, detail=session.detail)

    # ----------------------------------------------------------------- private

    def _require(self, worker_id: UUID) -> _Session:
        session = self._sessions.get(worker_id)
        if session is None:
            raise KeyError(f"Worker {worker_id} has no live session.")
        return session

    async def _launch(self, spec: WorkerSpec) -> WorkerHandle:
        session = _Session(spec=spec)
        self._sessions[spec.worker_id] = session
        ready: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        session.task = asyncio.create_task(self._run(session, ready))
        try:
            session_id = await asyncio.wait_for(ready, timeout=120)
        except TimeoutError as exc:
            session.alive = False
            session.detail = "Timed out connecting to the Claude runtime."
            raise RuntimeError(session.detail) from exc
        session.session_id = session_id
        return WorkerHandle(worker_id=spec.worker_id, session_id=session_id)

    def _options(self, spec: WorkerSpec) -> ClaudeAgentOptions:
        allowed = None if spec.writable else READ_ONLY_TOOLS
        disallowed = list(ALWAYS_DISALLOWED)
        if not spec.writable:
            disallowed += WRITE_TOOLS
        return ClaudeAgentOptions(
            cwd=str(spec.cwd),
            model=spec.model,
            resume=spec.resume_session_id,
            setting_sources=list(spec.setting_sources),  # type: ignore[arg-type]
            allowed_tools=allowed or [],
            disallowed_tools=disallowed,
            permission_mode="bypassPermissions" if spec.writable else "default",
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": spec.system_prompt_append,
            },
            # Workers get no MCP servers: the manager's tools and the global registry
            # are structurally unreachable from a worker session.
            mcp_servers={},
        )

    async def _run(self, session: _Session, ready: asyncio.Future[str | None]) -> None:
        spec = session.spec
        wid = spec.worker_id
        try:
            async with ClaudeSDKClient(options=self._options(spec)) as client:
                session.client = client
                if not ready.done():
                    ready.set_result(session.session_id)
                if spec.initial_prompt:
                    await self._turn(session, client, spec.initial_prompt)
                while session.alive:
                    message = await session.inbox.get()
                    if message is None:
                        break
                    await self._turn(session, client, message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            session.alive = False
            session.detail = str(exc)
            if not ready.done():
                ready.set_exception(exc)
                return
            await session.outbox.put(WorkerEvent(wid, "failed", f"Session error: {exc}"))

    async def _turn(self, session: _Session, client: ClaudeSDKClient, message: str) -> None:
        wid = session.spec.worker_id
        await client.query(message)
        final_text: list[str] = []
        async for msg in client.receive_response():
            if isinstance(msg, SystemMessage):
                sid = msg.data.get("session_id")
                if sid and sid != session.session_id:
                    session.session_id = sid
                    await session.outbox.put(WorkerEvent(wid, "session", sid))
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        final_text.append(block.text)
                        await session.outbox.put(WorkerEvent(wid, "text", block.text))
                    elif isinstance(block, ToolUseBlock):
                        if block.name == "Task":
                            session.helpers += 1
                            await session.outbox.put(
                                WorkerEvent(wid, "helper", "", {"active": session.helpers})
                            )
                        await session.outbox.put(
                            WorkerEvent(wid, "tool", block.name, {"input": block.input})
                        )
            elif isinstance(msg, ResultMessage):
                if msg.session_id and msg.session_id != session.session_id:
                    session.session_id = msg.session_id
                    await session.outbox.put(WorkerEvent(wid, "session", msg.session_id))
                text = "\n".join(final_text)
                kind: EventType = "blocked" if _looks_blocked(text) else "result"
                await session.outbox.put(
                    WorkerEvent(wid, kind, text, {"is_error": bool(msg.is_error)})
                )
            elif isinstance(msg, ToolResultBlock):  # pragma: no cover - defensive
                continue
        if session.helpers:
            session.helpers = 0
            await session.outbox.put(WorkerEvent(wid, "helper", "", {"active": 0}))


def _looks_blocked(text: str) -> bool:
    upper = (text or "").upper()
    return any(marker in upper for marker in BLOCKED_MARKERS)
