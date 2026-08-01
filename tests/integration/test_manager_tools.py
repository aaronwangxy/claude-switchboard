"""The native manager MCP is semantic, bounded, and generation-authorized."""

from __future__ import annotations

from uuid import uuid4

import pytest

from switchboard.agents.manager import DeterministicManager
from switchboard.agents.manager_mcp import TOOL_SCHEMAS, ManagerAuthorizationError, ManagerTools
from switchboard.domain.enums import RuntimeAgentKind, RuntimeOwner
from switchboard.domain.models import RuntimeInstance
from tests.conftest import TICKET


@pytest.fixture
def manager_tools(session_manager):
    manager_id = uuid4()
    runtime = RuntimeInstance(
        agent_id=manager_id,
        agent_kind=RuntimeAgentKind.MANAGER,
        backend="native-claude",
    )
    session_manager.store.save_runtime(runtime)
    return ManagerTools(session_manager, manager_id, runtime.id, runtime.generation), runtime


def test_manager_mcp_exposes_only_orchestration_semantics():
    assert set(TOOL_SCHEMAS) == {
        "register_repository",
        "create_job",
        "inspect_state",
        "list_workflows",
        "start_workflow",
        "start_run",
        "send_worker_followup",
        "record_decision",
        "resume_run",
        "interrupt_worker",
        "stop_worker",
        "inspect_contracts",
        "approve_plan",
        "status_summary",
    }
    assert not ({"bash", "read", "write", "edit", "create_worker"} & set(TOOL_SCHEMAS))


async def test_manager_can_route_first_class_workflow(manager_tools, git_repo):
    tools, _ = manager_tools
    repo = tools.sm.register_repository(git_repo("manager-route"))
    job = tools.sm.create_job("Plan", repo.id, ticket_text=TICKET)
    result = await tools.call(
        "start_workflow",
        {"workflow_name": "plan-feature", "job_id": str(job.id), "request": TICKET},
    )
    worker = tools.sm.store.get_worker(_uuid(result["worker_id"]))
    assert worker is not None and worker.workflow == "plan-feature"


async def test_stale_generation_loses_authority(manager_tools):
    tools, old = manager_tools
    replacement = RuntimeInstance(
        agent_id=old.agent_id,
        agent_kind=RuntimeAgentKind.MANAGER,
        generation=old.generation + 1,
        backend="native-claude",
    )
    tools.sm.store.save_runtime(replacement)
    with pytest.raises(ManagerAuthorizationError, match="no longer"):
        await tools.call("inspect_state", {})


async def test_human_ownership_closes_autonomous_tool_lane(manager_tools):
    tools, runtime = manager_tools
    runtime.owner = RuntimeOwner.HUMAN
    tools.sm.store.save_runtime(runtime)
    with pytest.raises(ManagerAuthorizationError):
        await tools.call("status_summary", {})


async def test_destructive_and_approval_tools_require_current_user_capability(manager_tools):
    tools, _ = manager_tools
    with pytest.raises(ValueError, match="confirmation"):
        await tools.call("stop_worker", {"worker_id": str(uuid4()), "confirmed": True})
    with pytest.raises(ValueError, match="explicit"):
        await tools.call("approve_plan", {"job_id": str(uuid4())})


async def test_fresh_manager_reconstructs_bounded_state(manager_tools, git_repo):
    tools, _ = manager_tools
    repo = tools.sm.register_repository(git_repo("reconstruct"))
    job = tools.sm.create_job("Durable objective", repo.id)
    tools.sm.store.set_preference("manager.current_objective", "Coordinate this safely")
    state = await tools.call("inspect_state", {})
    assert state["objective"] == "Coordinate this safely"
    assert any(item["id"] == str(job.id) for item in state["jobs"])
    assert "transcript" not in state


async def test_deterministic_manager_remains_offline_oracle(session_manager, git_repo):
    session_manager.register_repository(git_repo("offline"))
    reply = await DeterministicManager(session_manager).handle(TICKET)
    assert "ENG-421" in reply


def _uuid(value: str):
    from uuid import UUID

    return UUID(value)
