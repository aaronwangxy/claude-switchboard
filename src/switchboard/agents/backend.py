"""The backend boundary: orchestration never depends on SDK details directly."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

EventType = Literal[
    "session",  # session id captured
    "text",  # assistant text
    "tool",  # tool activity
    "helper",  # bounded subagent started/finished
    "blocked",  # worker is waiting on the user
    "permission",  # worker needs a permission/sandbox decision
    "result",  # turn finished
    "failed",  # worker errored
    "stopped",
]


@dataclass
class WorkerSpec:
    worker_id: UUID
    role: str
    cwd: Path
    system_prompt_append: str
    initial_prompt: str
    model: str | None = None
    writable: bool = False
    setting_sources: list[str] = field(default_factory=lambda: ["user", "project"])
    resume_session_id: str | None = None
    max_helpers: int = 3
    #: The Claude executable to launch, or None for the runtime's own default.
    claude_executable: str | None = None
    #: Extra environment for the session, merged over the inherited parent environment.
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class WorkerHandle:
    worker_id: UUID
    session_id: str | None = None
    started: bool = True


@dataclass
class WorkerEvent:
    worker_id: UUID
    type: EventType
    text: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class BackendHealth:
    alive: bool
    detail: str = ""


class WorkerBackend(Protocol):
    """Implemented by the Agent SDK backend and by the scripted test backend.

    A native `claude` CLI/PTY backend could be added behind this protocol later
    without touching orchestration.
    """

    async def start(self, spec: WorkerSpec) -> WorkerHandle: ...

    async def send(self, worker_id: UUID, message: str) -> None: ...

    def stream(self, worker_id: UUID) -> AsyncIterator[WorkerEvent]: ...

    async def interrupt(self, worker_id: UUID) -> None: ...

    async def stop(self, worker_id: UUID) -> None: ...

    async def resume(self, spec: WorkerSpec) -> WorkerHandle: ...

    async def health(self, worker_id: UUID) -> BackendHealth: ...
