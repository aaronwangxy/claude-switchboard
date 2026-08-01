"""The manager: a natural-language router over constrained in-process tools.

Two interchangeable implementations share one contract:

* `DeterministicManager` resolves and executes the route in ordinary Python. It needs no
  model, so tests and offline runs exercise the identical domain API.
* `ModelManager` gives a Claude session a bounded state snapshot plus the tools below and
  lets it choose. Every tool handler re-validates permissions, repository identity,
  worktree ownership, and state transitions, so a bad proposal cannot do damage.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import logging
import re
from typing import Any, Protocol
from uuid import UUID

from csm.agents.prompts import compose_manager_prompt
from csm.agents.runtime import claude_cli_path
from csm.agents.snapshots import Exchange, SnapshotInput, build_snapshot
from csm.core.session_manager import SessionManager, SessionManagerError
from csm.domain.enums import WorkerRole
from csm.routing import router
from csm.routing.router import RouteError
from csm.workflows.registry import WorkflowError, get_workflow, workflow_names

log = logging.getLogger(__name__)

CONFIRM_RE = re.compile(r"\b(yes,?\s*confirm|confirmed|do it anyway|yes,? proceed)\b", re.I)


class Manager(Protocol):
    async def handle(self, text: str) -> str: ...


class DeterministicManager:
    """Routes with the rule engine only. Same domain API as the model manager."""

    def __init__(self, session_manager: SessionManager) -> None:
        self.sm = session_manager
        self.exchanges: list[Exchange] = []

    async def handle(self, text: str) -> str:
        confirmed = bool(CONFIRM_RE.search(text))
        state = self.sm.routing_state(confirmed=confirmed)
        proposal = router.resolve_route(text, state)
        reply = await self.sm.execute_route(proposal, confirmed=confirmed)
        self.exchanges.append(Exchange(user=text, manager=reply))
        return reply


class ModelManager:
    """Invokes a Claude session per turn with a bounded snapshot and constrained tools."""

    def __init__(self, session_manager: SessionManager) -> None:
        self.sm = session_manager
        self.exchanges: list[Exchange] = []
        self._fallback = DeterministicManager(session_manager)
        #: Set per turn from the user's own words; a model claim of confirmation is not enough.
        self._user_confirmed = False
        #: The tool objects from the most recent `_tools()` build, keyed by name.
        self.tool_objects: dict[str, Any] = {}

    # ----------------------------------------------------------------- session

    def options(self) -> Any:
        """The manager's session options.

        Deliberately isolated: the manager runs in CSM's own data directory, never in a
        repository, and loads no setting sources -- so launching CSM from inside a
        repository does not quietly turn the router into that repository's coding agent.
        Its context is CSM's structured state, delivered as a snapshot each turn. It also
        has no file, shell, or subagent tools: it routes, it never works.
        """
        from claude_agent_sdk import ClaudeAgentOptions

        sm = self.sm
        return ClaudeAgentOptions(
            cwd=str(sm.store.path.parent),
            model=sm.config.models.manager,
            cli_path=claude_cli_path(sm.config.claude.executable),
            env=dict(sm.config.claude.env),
            setting_sources=[],
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": compose_manager_prompt(),
            },
            mcp_servers={"csm": self._tools()},
            allowed_tools=[f"mcp__csm__{name}" for name in MANAGER_TOOL_NAMES],
            disallowed_tools=["Bash", "Edit", "Write", "Read", "Glob", "Grep", "Task"],
            permission_mode="bypassPermissions",
            max_turns=12,
        )

    # ------------------------------------------------------------------- tools

    def _tools(self) -> Any:
        from claude_agent_sdk import create_sdk_mcp_server, tool

        sm = self.sm

        def ok(payload: Any) -> dict:
            return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}

        def err(message: str) -> dict:
            return {
                "content": [{"type": "text", "text": f"REFUSED: {message}"}],
                "is_error": True,
            }

        @tool("list_repositories", "List registered repositories.", {})
        async def list_repositories(args: dict) -> dict:
            return ok([{"id": str(r.id), "name": r.name, "path": str(r.root_path)} for r in sm.list_repositories()])

        @tool("register_repository", "Register a Git repository by path.", {"path": str, "name": str})
        async def register_repository(args: dict) -> dict:
            try:
                repo = sm.register_repository(args["path"], args.get("name") or None)
            except SessionManagerError as exc:
                return err(str(exc))
            return ok({"id": str(repo.id), "name": repo.name})

        @tool("list_jobs", "List jobs, optionally filtered by stage.", {"stage": str})
        async def list_jobs(args: dict) -> dict:
            jobs = sm.store.list_jobs()
            stage = args.get("stage")
            return ok(
                [
                    {"id": str(j.id), "ref": j.external_ref, "title": j.title, "stage": j.stage.value}
                    for j in jobs
                    if not stage or j.stage.value == stage
                ]
            )

        @tool("list_workers", "List workers, optionally for one job.", {"job_id": str})
        async def list_workers(args: dict) -> dict:
            job_id = _uuid(args.get("job_id"))
            return ok(
                [
                    {
                        "id": str(w.id),
                        "title": w.title,
                        "role": w.role.value,
                        "status": w.status.value,
                        "writable": w.writable,
                        "waiting_for": w.waiting_for,
                    }
                    for w in sm.store.list_workers(job_id)
                ]
            )

        @tool("inspect_worker", "Inspect one worker's state and recent output.", {"worker_id": str})
        async def inspect_worker(args: dict) -> dict:
            worker = sm.store.get_worker(_require_uuid(args["worker_id"]))
            if worker is None:
                return err("No such worker.")
            tail = sm.store.transcript(worker.id)[-4:]
            return ok(
                {
                    "id": str(worker.id),
                    "title": worker.title,
                    "role": worker.role.value,
                    "status": worker.status.value,
                    "cwd": str(worker.cwd),
                    "waiting_for": worker.waiting_for,
                    "recent": [f"{m.role}: {m.text[:200]}" for m in tail],
                }
            )

        @tool(
            "create_job",
            "Create a job for a ticket or request.",
            {"title": str, "repository_id": str, "external_ref": str, "ticket_text": str},
        )
        async def create_job(args: dict) -> dict:
            try:
                job = sm.create_job(
                    title=args["title"],
                    repository_id=_require_uuid(args["repository_id"]),
                    external_ref=args.get("external_ref") or None,
                    ticket_text=args.get("ticket_text", ""),
                )
            except (SessionManagerError, ValueError) as exc:
                return err(str(exc))
            return ok({"id": str(job.id), "title": job.title})

        @tool(
            "create_worker",
            "Create an independent worker session. Prefer start_workflow, which creates the "
            f"right worker for you. role must be one of: {ROLE_VALUES}.",
            {"role": str, "title": str, "prompt": str, "job_id": str, "repository_id": str, "writable": bool},
        )
        async def create_worker(args: dict) -> dict:
            role = args.get("role")
            if role not in _ROLE_SET:
                return err(f"{role!r} is not a role. Use one of: {ROLE_VALUES}.")
            try:
                worker = await sm.create_worker(
                    role=WorkerRole(role),
                    title=args["title"],
                    prompt=args.get("prompt", ""),
                    job_id=_uuid(args.get("job_id")),
                    repository_id=_uuid(args.get("repository_id")),
                    writable=args.get("writable"),
                )
            except (SessionManagerError, ValueError) as exc:
                return err(str(exc))
            return ok({"id": str(worker.id), "cwd": str(worker.cwd), "writable": worker.writable})

        @tool("route_message", "Send a message to an existing worker.", {"worker_id": str, "message": str})
        async def route_message(args: dict) -> dict:
            try:
                await sm.send(_require_uuid(args["worker_id"]), args["message"])
            except (SessionManagerError, ValueError) as exc:
                return err(str(exc))
            return ok({"sent": True})

        @tool(
            "start_workflow",
            "Run a reusable workflow on a job. Creates the worker the workflow needs, in the "
            "right worktree, unless you target an existing one. workflow_name must be one of "
            "the workflows listed in the snapshot.",
            {"workflow_name": str, "job_id": str, "target_worker_id": str, "request": str},
        )
        async def start_workflow(args: dict) -> dict:
            try:
                get_workflow(args.get("workflow_name") or "")
            except WorkflowError as exc:
                return err(str(exc))
            try:
                worker = await sm.start_workflow(
                    args["workflow_name"],
                    job_id=_uuid(args.get("job_id")),
                    target_worker_id=_uuid(args.get("target_worker_id")),
                    request=args.get("request", ""),
                )
            except (SessionManagerError, ValueError) as exc:
                return err(str(exc))
            return ok({"worker_id": str(worker.id), "title": worker.title})

        @tool("open_worker", "Select a worker in the right-hand pane.", {"worker_id": str})
        async def open_worker(args: dict) -> dict:
            worker_id = _require_uuid(args["worker_id"])
            if sm.store.get_worker(worker_id) is None:
                return err("No such worker.")
            sm.selected_worker_id = worker_id
            return ok({"selected": str(worker_id)})

        @tool("interrupt_worker", "Interrupt a worker's current turn.", {"worker_id": str})
        async def interrupt_worker(args: dict) -> dict:
            try:
                await sm.interrupt_worker(_require_uuid(args["worker_id"]))
            except (SessionManagerError, KeyError, ValueError) as exc:
                return err(str(exc))
            return ok({"interrupted": True})

        @tool("stop_worker", "Stop a worker session.", {"worker_id": str})
        async def stop_worker(args: dict) -> dict:
            try:
                await sm.stop_worker(_require_uuid(args["worker_id"]))
            except (SessionManagerError, ValueError) as exc:
                return err(str(exc))
            return ok({"stopped": True})

        @tool(
            "request_cleanup",
            "Remove a worker's worktree. Destructive: only pass confirmed=true when the "
            "user explicitly confirmed in their latest message.",
            {"worker_id": str, "job_id": str, "confirmed": bool},
        )
        async def request_cleanup(args: dict) -> dict:
            try:
                result = await sm.request_cleanup(
                    worker_id=_uuid(args.get("worker_id")),
                    job_id=_uuid(args.get("job_id")),
                    confirmed=bool(args.get("confirmed")) and self._user_confirmed,
                )
            except (SessionManagerError, ValueError) as exc:
                return err(str(exc))
            return ok({"performed": result.performed, "explanation": result.decision.explanation})

        @tool("list_attention_items", "List what needs the user, highest priority first.", {})
        async def list_attention_items(args: dict) -> dict:
            return ok(
                [
                    {"worker_id": str(i.worker_id), "kind": i.kind.value, "reason": i.reason}
                    for i in sm.list_attention_items()
                ]
            )

        @tool("record_decision", "Record the user's answer to a job decision.", {"job_id": str, "question": str, "answer": str})
        async def record_decision(args: dict) -> dict:
            try:
                sm.record_decision(_require_uuid(args["job_id"]), args["question"], args["answer"])
            except (SessionManagerError, ValueError) as exc:
                return err(str(exc))
            return ok({"recorded": True})

        registered = [
            list_repositories,
            register_repository,
            list_jobs,
            list_workers,
            inspect_worker,
            create_job,
            create_worker,
            route_message,
            start_workflow,
            open_worker,
            interrupt_worker,
            stop_worker,
            request_cleanup,
            list_attention_items,
            record_decision,
        ]
        # A malformed argument must come back as a refusal the manager can read and
        # correct, never as an exception that kills the turn.
        guarded = [dataclasses.replace(t, handler=_guard(t.handler, err)) for t in registered]
        self.tool_objects = {t.name: t for t in guarded}
        return create_sdk_mcp_server(name="csm", version="1.0.0", tools=guarded)

    # ------------------------------------------------------------------ handle

    async def handle(self, text: str) -> str:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeSDKClient,
            TextBlock,
        )

        self._user_confirmed = bool(CONFIRM_RE.search(text))
        sm = self.sm

        # Destructive routes are gated deterministically before the model is ever asked,
        # so no amount of model confidence can skip the confirmation.
        state = sm.routing_state(confirmed=self._user_confirmed)
        try:
            proposal = router.resolve_route(text, state)
        except RouteError as exc:
            return f"Refused: {exc}"
        if proposal.action == "confirm_destructive":
            reply = proposal.question or proposal.reason
            self.exchanges.append(Exchange(user=text, manager=reply))
            return reply

        snapshot = build_snapshot(
            SnapshotInput(
                repositories=sm.store.list_repositories(),
                jobs=sm.store.list_jobs(),
                workers=sm.store.list_workers(),
                attention=sm.list_attention_items(),
                events=sm.store.recent_events(),
                exchanges=self.exchanges,
                selected_worker_id=sm.selected_worker_id,
                selected_job_id=state.selected_job_id,
            ),
            route=proposal,
        )
        options = self.options()
        parts: list[str] = []
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(f"{snapshot}\n\n## User request\n{text}")
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock) and block.text.strip():
                                parts.append(block.text.strip())
        except Exception as exc:
            log.warning("manager model turn failed (%s); using the deterministic router", exc)
            return await self._fallback.handle(text)

        reply = "\n".join(parts).strip() or sm.status_summary()
        self.exchanges.append(Exchange(user=text, manager=reply))
        return reply


ROLE_VALUES = ", ".join(r.value for r in WorkerRole)
_ROLE_SET = {r.value for r in WorkerRole}


def workflow_values() -> str:
    """The live workflow list, so a user-defined workflow is offered like any other."""
    return ", ".join(workflow_names())

MANAGER_TOOL_NAMES = [
    "list_repositories",
    "register_repository",
    "list_jobs",
    "list_workers",
    "inspect_worker",
    "create_job",
    "create_worker",
    "route_message",
    "start_workflow",
    "open_worker",
    "interrupt_worker",
    "stop_worker",
    "request_cleanup",
    "list_attention_items",
    "record_decision",
]


def _guard(handler: Any, err: Any) -> Any:
    """Turn any handler failure into a refusal message naming what went wrong."""

    @functools.wraps(handler)
    async def wrapper(args: dict) -> dict:
        try:
            return await handler(args)
        except (SessionManagerError, ValueError, KeyError, TypeError) as exc:
            return err(str(exc) or exc.__class__.__name__)

    return wrapper


def _require_uuid(value: Any) -> UUID:
    """A tool argument that must be a UUID. Malformed input raises, and the handler refuses."""
    parsed = _uuid(value)
    if parsed is None:
        raise ValueError("A valid id is required.")
    return parsed


def _uuid(value: Any) -> UUID | None:
    if not value:
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))
