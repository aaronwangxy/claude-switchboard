"""Generation-authorized MCP tools for the native Switchboard manager.

The stdio process is launched only by the manager's Claude process.  Every call checks
the durable generation again, so inheriting an old pipe is not enough to retain authority.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from switchboard.agents.native_backend import NativeClaudeBackend
from switchboard.config import Config, load_config, worktree_root
from switchboard.core.session_manager import SessionManager
from switchboard.domain.enums import RuntimeAgentKind
from switchboard.gitops.worktrees import WorktreeService
from switchboard.storage.store import Store
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
        jobs = self.sm.store.list_jobs()[:12]
        workers = self.sm.store.list_workers()[-20:]
        runs = self.sm.store.list_runs()[-12:]
        return {
            "objective": self.sm.store.get_preference("manager.current_objective", ""),
            "jobs": [{"id": str(j.id), "title": j.title, "stage": j.stage.value} for j in jobs],
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
            "role": workflow.default_role.value,
            "requires": [item.value for item in workflow.requires],
            "produces": [item.value for item in workflow.produces],
            "mutates_code": workflow.mutates_code,
            "steps": [step.workflow for step in workflow.steps],
        }


def _uuid(value: Any) -> UUID | None:
    return UUID(str(value)) if value else None


async def _serve(tools: ManagerTools) -> None:
    # The MCP process owns pumps for workers it creates. After manager rotation/crash the
    # replacement adopts those same native workers before accepting another tool call.
    await tools.sm.recover()
    loop = asyncio.get_running_loop()
    while line := await loop.run_in_executor(None, sys.stdin.readline):
        request = json.loads(line)
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
                continue
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}) + "\n")
        sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--manager-id", type=UUID, required=True)
    parser.add_argument("--runtime-id", type=UUID, required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    ns = parser.parse_args(argv)
    store = Store(ns.database)
    config: Config = load_config()
    backend = NativeClaudeBackend(store, config, ns.state_dir)
    sm = SessionManager(
        store, backend, config, WorktreeService(worktree_root(), config.worktree_bootstrap.files)
    )
    sm.reload_workflows()
    try:
        asyncio.run(_serve(ManagerTools(sm, ns.manager_id, ns.runtime_id, ns.generation)))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
