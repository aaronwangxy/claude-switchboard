"""A deterministic in-process backend.

Used by the test suite and by `CSM_BACKEND=scripted` so the whole control plane --
routing, worktrees, contracts, attention, invalidation -- can be exercised without
calling a model. It emits the same normalized events as the SDK backend.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from switchboard.agents.backend import BackendHealth, WorkerEvent, WorkerHandle, WorkerSpec

Responder = Callable[[WorkerSpec, str], list[WorkerEvent]]


def _plan_response(spec: WorkerSpec, message: str) -> str:
    contract = {
        "summary_lines": [
            "Add a notification preferences table.",
            "Persist preferences on save.",
            "Read preferences in the dispatcher.",
        ],
        "decisions": [
            {
                "id": "D1",
                "question": "Must legacy records remain writable?",
                "options": [
                    "Yes, keep legacy writes",
                    "Read legacy, write new format only",
                    "Drop legacy support",
                ],
                "recommendation": "Read legacy, write new format only",
                "blocking": True,
            }
        ],
        "commit_stack": [
            {"order": 1, "message": "feat: add preferences table", "purpose": "schema"},
            {"order": 2, "message": "feat: persist preferences", "purpose": "write path"},
        ],
        "risks": ["Legacy rows may lack a preferences column."],
        "base_commit": "",
        "criteria": [
            {
                "id": "AC1",
                "behavior": "Saving preferences persists them across a restart.",
                "verification_method": "integration test",
                "evidence_required": ["pytest exit code", "database read"],
            }
        ],
    }
    return (
        "Plan ready. One decision blocks implementation.\n\n```json\n"
        + json.dumps(contract, indent=2)
        + "\n```\n[NEEDS DECISION] Choose the legacy-write strategy."
    )


def _verify_response(spec: WorkerSpec, message: str) -> str:
    report = {
        "scope": "full",
        "evidence": [
            {
                "criterion_id": "AC1",
                "status": "passed",
                "commands": [{"command": "pytest -q", "exit_code": 0, "output_excerpt": "3 passed"}],
                "observed_behavior": "Preferences survived a restart of the store.",
                "artifacts": [],
                "limitations": [],
            }
        ],
    }
    return "Pass. AC1 verified.\n\n```json\n" + json.dumps(report, indent=2) + "\n```"


def _review_response(spec: WorkerSpec, message: str) -> str:
    report = {"verdict": "pass", "findings": []}
    return "Pass. No blocking findings.\n\n```json\n" + json.dumps(report, indent=2) + "\n```"


def _implement_response(spec: WorkerSpec, message: str) -> str:
    return "Implemented the approved stack in 2 commits; focused tests pass."


def _default_response(spec: WorkerSpec, message: str) -> str:
    return f"Acknowledged: {message.splitlines()[0][:80] if message else 'started'}"


ROLE_RESPONSES: dict[str, Callable[[WorkerSpec, str], str]] = {
    "planner": _plan_response,
    "verifier": _verify_response,
    "reviewer": _review_response,
    "implementer": _implement_response,
}


@dataclass
class _Session:
    spec: WorkerSpec
    session_id: str
    outbox: asyncio.Queue[WorkerEvent | None] = field(default_factory=asyncio.Queue)
    alive: bool = True
    interrupts: int = 0
    messages: list[str] = field(default_factory=list)


class ScriptedWorkerBackend:
    """Deterministic stand-in for the SDK backend.

    `responses` maps a role name to a callable returning the assistant text for a turn.
    Tests override entries to drive specific scenarios (blocked workers, failures).
    """

    def __init__(self, responses: dict[str, Callable[[WorkerSpec, str], str]] | None = None) -> None:
        self.responses = dict(ROLE_RESPONSES)
        if responses:
            self.responses.update(responses)
        self._sessions: dict[UUID, _Session] = {}
        self.started: list[WorkerSpec] = []
        self.stopped: list[UUID] = []

    def role_of(self, spec: WorkerSpec) -> str:
        return spec.role

    async def start(self, spec: WorkerSpec) -> WorkerHandle:
        session = _Session(spec=spec, session_id=spec.resume_session_id or f"scripted-{uuid4()}")
        self._sessions[spec.worker_id] = session
        self.started.append(spec)
        await session.outbox.put(WorkerEvent(spec.worker_id, "session", session.session_id))
        if spec.initial_prompt:
            await self._turn(session, spec.initial_prompt)
        return WorkerHandle(worker_id=spec.worker_id, session_id=session.session_id)

    async def resume(self, spec: WorkerSpec) -> WorkerHandle:
        return await self.start(spec)

    async def send(self, worker_id: UUID, message: str) -> None:
        await self._turn(self._require(worker_id), message)

    async def stream(self, worker_id: UUID) -> AsyncIterator[WorkerEvent]:
        session = self._require(worker_id)
        while True:
            event = await session.outbox.get()
            if event is None:
                return
            yield event

    async def interrupt(self, worker_id: UUID) -> None:
        session = self._require(worker_id)
        session.interrupts += 1
        await session.outbox.put(WorkerEvent(worker_id, "result", "Interrupted."))

    async def stop(self, worker_id: UUID) -> None:
        session = self._sessions.pop(worker_id, None)
        self.stopped.append(worker_id)
        if session is None:
            return
        session.alive = False
        await session.outbox.put(WorkerEvent(worker_id, "stopped", "Session stopped."))
        await session.outbox.put(None)

    async def health(self, worker_id: UUID) -> BackendHealth:
        session = self._sessions.get(worker_id)
        if session is None:
            return BackendHealth(alive=False, detail="No live session for this worker.")
        return BackendHealth(alive=session.alive, detail="scripted")

    def _require(self, worker_id: UUID) -> _Session:
        session = self._sessions.get(worker_id)
        if session is None:
            raise KeyError(f"Worker {worker_id} has no live session.")
        return session

    async def _turn(self, session: _Session, message: str) -> None:
        session.messages.append(message)
        responder = self.responses.get(self.role_of(session.spec), _default_response)
        text = responder(session.spec, message)
        wid = session.spec.worker_id
        await session.outbox.put(WorkerEvent(wid, "text", text))
        upper = text.upper()
        if "[NEEDS INPUT]" in upper or "[NEEDS DECISION]" in upper:
            await session.outbox.put(WorkerEvent(wid, "blocked", text))
        elif "[FAILED]" in upper:
            await session.outbox.put(WorkerEvent(wid, "failed", text))
        else:
            await session.outbox.put(WorkerEvent(wid, "result", text))
