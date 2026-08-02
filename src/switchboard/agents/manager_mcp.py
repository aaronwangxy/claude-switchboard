"""Generation-authorized MCP tools for the native Switchboard manager.

The stdio process is launched only by the manager's Claude process.  Every call checks
the durable generation again, so inheriting an old pipe is not enough to retain authority.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from switchboard.core.session_manager import SessionManager
from switchboard.domain.enums import RuntimeAgentKind, RuntimeProcessState
from switchboard.workflows.registry import get_workflow, workflow_names

TOOL_SCHEMAS: dict[str, tuple[str, dict[str, Any]]] = {
    "register_repository": (
        "Register a user-specified Git repository path.",
        {
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string"}, "name": {"type": "string"}},
        },
    ),
    "create_job": (
        "Create durable work for a goal in a registered repository.",
        {
            "type": "object",
            "required": ["title", "repository_id"],
            "properties": {
                "title": {"type": "string"},
                "repository_id": {"type": "string"},
                "external_ref": {"type": "string"},
                "ticket_text": {"type": "string"},
            },
        },
    ),
    "inspect_state": (
        "Inspect bounded authoritative objectives, jobs, runs, workers and attention.",
        {},
    ),
    "list_workflows": ("List first-class workflows and whether each is composite.", {}),
    "start_workflow": (
        "Start an atomic workflow for a job.",
        {
            "type": "object",
            "required": ["workflow_name", "job_id"],
            "properties": {
                "workflow_name": {"type": "string"},
                "job_id": {"type": "string"},
                "request": {"type": "string"},
                "target_worker_id": {"type": "string"},
            },
        },
    ),
    "start_run": (
        "Start a durable composite workflow run.",
        {
            "type": "object",
            "required": ["workflow_name", "job_id"],
            "properties": {
                "workflow_name": {"type": "string"},
                "job_id": {"type": "string"},
                "request": {"type": "string"},
            },
        },
    ),
    "send_worker_followup": (
        "Send a follow-up to an existing worker.",
        {
            "type": "object",
            "required": ["worker_id", "message"],
            "properties": {"worker_id": {"type": "string"}, "message": {"type": "string"}},
        },
    ),
    "record_decision": (
        "Record a user correction, priority, or durable decision for a job.",
        {"type": "object", "required": ["job_id", "question", "answer"], "properties": {"job_id": {"type": "string"}, "question": {"type": "string"}, "answer": {"type": "string"}}},
    ),
    "resume_run": (
        "Resume a paused durable workflow run.",
        {"type": "object", "required": ["run_id"], "properties": {"run_id": {"type": "string"}}},
    ),
    "interrupt_worker": (
        "Interrupt the current worker turn without destroying its process.",
        {
            "type": "object",
            "required": ["worker_id"],
            "properties": {"worker_id": {"type": "string"}},
        },
    ),
    "stop_worker": (
        "Stop a worker after explicit user confirmation.",
        {
            "type": "object",
            "required": ["worker_id", "confirmed"],
            "properties": {"worker_id": {"type": "string"}, "confirmed": {"type": "boolean"}},
        },
    ),
    "trust_repository_worktrees": (
        "Record that the user vouches for Switchboard's worktrees of a repository, so "
        "its workers stop asking about workspace trust. Needs explicit user confirmation.",
        {
            "type": "object",
            "required": ["repository_id", "confirmed"],
            "properties": {
                "repository_id": {"type": "string"},
                "confirmed": {"type": "boolean"},
            },
        },
    ),
    "unblock_worker_startup": (
        "Answer a worker's native workspace-trust prompt, if that is what it is waiting "
        "on and the user has vouched for the repository.",
        {
            "type": "object",
            "required": ["worker_id"],
            "properties": {"worker_id": {"type": "string"}, "confirmed": {"type": "boolean"}},
        },
    ),
    "inspect_contracts": (
        "Inspect bounded contracts and evidence for a job.",
        {"type": "object", "required": ["job_id"], "properties": {"job_id": {"type": "string"}}},
    ),
    "approve_plan": (
        "Approve the current implementation contract when user policy permits.",
        {"type": "object", "required": ["job_id"], "properties": {"job_id": {"type": "string"}}},
    ),
    "status_summary": ("Report current durable orchestration status.", {}),
}


class ManagerAuthorizationError(RuntimeError):
    pass


class ManagerTools:
    def __init__(
        self, sm: SessionManager, manager_id: UUID, runtime_id: UUID, generation: int
    ) -> None:
        self.sm = sm
        self.manager_id = manager_id
        self.runtime_id = runtime_id
        self.generation = generation

    def authorize(self) -> None:
        current = self.sm.store.current_runtime(self.manager_id)
        if (
            current is None
            or current.id != self.runtime_id
            or current.generation != self.generation
            or current.agent_kind is not RuntimeAgentKind.MANAGER
            or current.process_state is RuntimeProcessState.EXITED
        ):
            raise ManagerAuthorizationError("This manager generation no longer has authority.")

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        self.authorize()
        if name == "register_repository":
            repo = self.sm.register_repository(args["path"], args.get("name") or None)
            return {"repository_id": str(repo.id), "name": repo.name}
        if name == "create_job":
            job = self.sm.create_job(
                args["title"],
                UUID(args["repository_id"]),
                external_ref=args.get("external_ref") or None,
                ticket_text=args.get("ticket_text", ""),
            )
            return {"job_id": str(job.id), "title": job.title}
        if name == "inspect_state":
            return self._state()
        if name == "list_workflows":
            return [self._workflow(n) for n in workflow_names()]
        if name == "start_workflow":
            worker = await self.sm.start_workflow(
                args["workflow_name"],
                job_id=UUID(args["job_id"]),
                target_worker_id=_uuid(args.get("target_worker_id")),
                request=args.get("request", ""),
            )
            return {"worker_id": str(worker.id), "workflow": worker.workflow}
        if name == "start_run":
            run = await self.sm.start_run(
                args["workflow_name"], job_id=UUID(args["job_id"]), request=args.get("request", "")
            )
            return {"run_id": str(run.id), "status": run.status.value}
        if name == "send_worker_followup":
            await self.sm.send(UUID(args["worker_id"]), args["message"])
            return {"sent": True}
        if name == "record_decision":
            decision = self.sm.record_decision(UUID(args["job_id"]), args["question"], args["answer"])
            return {"decision_id": str(decision.id)}
        if name == "resume_run":
            run = await self.sm.resume_run(UUID(args["run_id"]))
            return {"run_id": str(run.id), "status": run.status.value}
        if name == "interrupt_worker":
            await self.sm.interrupt_worker(UUID(args["worker_id"]))
            return {"interrupted": True}
        if name == "stop_worker":
            turn = self.sm.store.open_native_turn(self.runtime_id)
            confirmed_turn = self.sm.store.get_preference("manager.confirmed_turn", "")
            if not args.get("confirmed") or turn is None or confirmed_turn != str(turn.id):
                raise ValueError("Explicit confirmation is required.")
            await self.sm.stop_worker(UUID(args["worker_id"]))
            return {"stopped": True}
        if name == "trust_repository_worktrees":
            turn = self.sm.store.open_native_turn(self.runtime_id)
            confirmed_turn = self.sm.store.get_preference("manager.confirmed_turn", "")
            if not args.get("confirmed") or turn is None or confirmed_turn != str(turn.id):
                raise ValueError(
                    "Trusting a repository's worktrees needs the user's confirmation in "
                    "their current message."
                )
            self.sm.grant_repository_trust(UUID(args["repository_id"]), confirmed=True)
            return {"trusted": True}
        if name == "unblock_worker_startup":
            answered = await self.sm.answer_workspace_trust(
                UUID(args["worker_id"]), confirmed=bool(args.get("confirmed"))
            )
            return {"answered": answered}
        if name == "inspect_contracts":
            job_id = UUID(args["job_id"])
            return [
                {"type": a.type.value, "stale": a.stale, "body": a.body}
                for a in self.sm.store.list_artifacts(job_id)
            ][-12:]
        if name == "approve_plan":
            turn = self.sm.store.open_native_turn(self.runtime_id)
            approved_turn = self.sm.store.get_preference("manager.approval_turn", "")
            if turn is None or approved_turn != str(turn.id):
                raise ValueError("Plan approval must be explicit in the current user turn.")
            artifact = self.sm.approve_plan(UUID(args["job_id"]))
            return {"approved": str(artifact.id)}
        if name == "status_summary":
            return self.sm.status_summary()
        raise ValueError(f"Unknown manager tool: {name}")

    def _state(self) -> dict[str, Any]:
        repositories = self.sm.store.list_repositories()[:12]
        jobs = self.sm.store.list_jobs()[:12]
        workers = self.sm.store.list_workers()[-20:]
        runs = self.sm.store.list_runs()[-12:]
        return {
            "objective": self.sm.store.get_preference("manager.current_objective", ""),
            "repositories": [
                {"id": str(repo.id), "name": repo.name, "path": str(repo.root_path)}
                for repo in repositories
            ],
            "jobs": [{"id": str(j.id), "title": j.title, "stage": j.stage} for j in jobs],
            "runs": [
                {
                    "id": str(r.id),
                    "job_id": str(r.job_id),
                    "workflow": r.workflow,
                    "status": r.status.value,
                    "detail": r.detail[:300],
                    "step_index": r.step_index,
                    "approved_steps": r.approved_steps,
                }
                for r in runs
            ],
            "workers": [
                {
                    "id": str(w.id),
                    "job_id": str(w.job_id) if w.job_id else None,
                    "title": w.title,
                    "role": w.role.value,
                    "status": w.status.value,
                    "waiting_for": w.waiting_for,
                }
                for w in workers
            ],
            "attention": [
                {"worker_id": str(i.worker_id), "kind": i.kind.value, "reason": i.reason[:300]}
                for i in self.sm.list_attention_items()[:12]
            ],
            "recent_decisions": [
                {
                    "job_id": str(j.id),
                    "decisions": [
                        {"question": d.question, "answer": d.answer}
                        for d in self.sm.store.list_decisions(j.id)[-3:]
                    ],
                }
                for j in jobs
            ],
            "contracts_evidence": [
                {
                    "job_id": str(j.id),
                    "artifacts": [
                        {
                            "type": a.type.value,
                            "stale": a.stale,
                            "summary": json.dumps(a.body, default=str)[:500],
                        }
                        for a in self.sm.store.list_artifacts(j.id)[-4:]
                    ],
                }
                for j in jobs
            ],
            "handoff": self.sm.store.get_preference(
                f"manager.handoff.{self.runtime_id}", ""
            ),
        }

    def _workflow(self, name: str) -> dict[str, Any]:
        workflow = get_workflow(name)
        return {
            "name": name,
            "description": workflow.description,
            "composite": workflow.is_composite,
            "role": workflow.role.value,
            "requires": [item.value for item in workflow.requires],
            "produces": [item.value for item in workflow.produces],
            "mutates_code": workflow.mutates_code,
            "steps": [step.workflow for step in workflow.steps],
        }


def _uuid(value: Any) -> UUID | None:
    return UUID(str(value)) if value else None


async def handle_request(tools: ManagerTools, request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    ident = request.get("id")
    result: Any
    if method == "initialize":
        result = {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "switchboard", "version": "1.0.0"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": name,
                    "description": spec[0],
                    "inputSchema": spec[1] or {"type": "object", "properties": {}},
                }
                for name, spec in TOOL_SCHEMAS.items()
            ]
        }
    elif method == "tools/call":
        try:
            payload = await tools.call(
                request["params"]["name"], request["params"].get("arguments", {})
            )
            result = {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}
        except Exception as exc:
            result = {"content": [{"type": "text", "text": f"REFUSED: {exc}"}], "isError": True}
    else:
        if ident is None:
            return None
        result = {}
    return {"jsonrpc": "2.0", "id": ident, "result": result}


async def serve_connection(
    tools: ManagerTools, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        while line := await reader.readline():
            response = await handle_request(tools, json.loads(line))
            if response is not None:
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


#: How long a bridge waits for the board's socket while the controller is restarting.
RECONNECT_TIMEOUT = 30.0


class _AppliedUnknownError(ConnectionResetError):
    """The board took the request and vanished, so its effect cannot be assumed either way."""


class _Bridge:
    """A stdio-to-socket bridge that outlives the board controller.

    The board owns the Unix socket, so quitting it closes this connection. Claude Code
    never respawns a stdio MCP server that exits, so dying here would silently strip a
    surviving native Manager of every orchestration tool for the rest of its session --
    leaving a model that narrates tool calls it cannot make. The board rebinds the same
    generation-bound path when it comes back, and every call is reauthorized server-side,
    so reconnecting is both safe and enough to restore authority.
    """

    def __init__(self, socket_path: Path, timeout: float = RECONNECT_TIMEOUT) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bool]:
        if self._reader is not None and self._writer is not None:
            if not self._writer.is_closing() and not self._reader.at_eof():
                return self._reader, self._writer, True
            self._drop()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        delay = 0.05
        while True:
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(self.socket_path)
            except OSError:
                if loop.time() >= deadline:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 1.0)
            else:
                return self._reader, self._writer, False

    def _drop(self) -> None:
        if self._writer is not None:
            self._writer.close()
        self._reader = None
        self._writer = None

    async def exchange(self, line: str, wants_response: bool) -> str | None:
        for attempt in (0, 1):
            reader, writer, reused = await self._open()
            try:
                writer.write(line.encode())
                await writer.drain()
            except OSError:
                self._drop()
                # A reused connection that fails to write has almost certainly delivered
                # nothing, so one resend is worth it rather than failing a whole turn.
                # `drain` can in principle report a loss after the bytes were flushed, so
                # this is the one place the bridge is not provably exactly-once.
                if attempt == 0 and reused:
                    continue
                raise
            if not wants_response:
                return None
            response = await reader.readline()
            if response:
                return response.decode()
            # The board accepted the request and then vanished, so it may already have been
            # applied. Never resend, and say so: telling the Manager this never reached
            # Switchboard would invite it to retry a mutation that already happened.
            self._drop()
            raise _AppliedUnknownError(
                "the Switchboard board closed the connection after accepting this call"
            )
        raise ConnectionResetError("the Switchboard board is unreachable")

    async def close(self) -> None:
        writer = self._writer
        self._drop()
        if writer is not None:
            try:
                await writer.wait_closed()
            except OSError:
                pass


def _unreachable(ident: Any, exc: Exception) -> str:
    if isinstance(exc, _AppliedUnknownError):
        message = (
            f"Switchboard accepted this call and then became unreachable: {exc}. Its effect "
            "is unknown. Inspect authoritative state before retrying; do not assume it "
            "failed."
        )
    else:
        message = (
            f"Switchboard is not reachable: {exc}. Nothing was applied; retry once the "
            "board is running."
        )
    return json.dumps({"jsonrpc": "2.0", "id": ident, "error": {"code": -32000, "message": message}})


async def _proxy(socket_path: Path, timeout: float = RECONNECT_TIMEOUT) -> None:
    bridge = _Bridge(socket_path, timeout)
    loop = asyncio.get_running_loop()
    try:
        while line := await loop.run_in_executor(None, sys.stdin.readline):
            request = json.loads(line)
            ident = request.get("id")
            try:
                response = await bridge.exchange(line, ident is not None)
            except OSError as exc:
                if ident is None:
                    continue
                response = _unreachable(ident, exc) + "\n"
            if response is not None:
                sys.stdout.write(response)
                sys.stdout.flush()
    finally:
        await bridge.close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, required=True)
    ns = parser.parse_args(argv)
    asyncio.run(_proxy(ns.socket))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
