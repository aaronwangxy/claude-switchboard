"""The manager's constrained tool surface, exercised without invoking a model."""

from __future__ import annotations

import asyncio
import json

import pytest

from switchboard.agents.manager import MANAGER_TOOL_NAMES, DeterministicManager, ModelManager
from switchboard.domain.enums import WorkerRole, WorkerStatus
from tests.conftest import TICKET


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)


@pytest.fixture
def tools(session_manager):
    """The manager's in-process tools, callable directly."""
    manager = ModelManager(session_manager)
    manager._tools()
    return manager, manager.tool_objects


async def call(tools_by_name, tool_name, **args):
    result = await tools_by_name[tool_name].handler(args)
    text = result["content"][0]["text"]
    if result.get("is_error"):
        return {"refused": text}
    return json.loads(text)


def test_every_specified_manager_tool_is_registered(tools):
    _, by_name = tools
    assert set(by_name) == set(MANAGER_TOOL_NAMES)
    for expected in (
        "register_repository",
        "list_repositories",
        "list_jobs",
        "list_workers",
        "inspect_worker",
        "create_job",
        "create_worker",
        "route_message",
        "open_worker",
        "interrupt_worker",
        "stop_worker",
        "request_cleanup",
        "list_attention_items",
        "record_decision",
        "start_workflow",
    ):
        assert expected in by_name


async def test_the_manager_can_drive_a_worker_through_its_whole_life(tools, git_repo):
    manager, by_name = tools
    sm = manager.sm
    repo_path = git_repo("alpha")

    registered = await call(by_name, "register_repository", path=str(repo_path), name="alpha")
    assert registered["name"] == "alpha"
    assert await call(by_name, "list_repositories")

    job = await call(
        by_name, "create_job", title="Preferences", repository_id=registered["id"],
        external_ref="ENG-421", ticket_text=TICKET,
    )
    assert any(j["ref"] == "ENG-421" for j in await call(by_name, "list_jobs"))

    started = await call(
        by_name, "start_workflow", workflow_name="plan-feature", job_id=job["id"], request=TICKET
    )
    await settle()
    worker_id = started["worker_id"]

    listed = await call(by_name, "list_workers", job_id=job["id"])
    assert listed[0]["role"] == "planner" and listed[0]["writable"] is False

    inspected = await call(by_name, "inspect_worker", worker_id=worker_id)
    assert inspected["status"] == "blocked"
    assert inspected["recent"], "inspect returns a bounded transcript tail, not the whole thing"

    assert (await call(by_name, "open_worker", worker_id=worker_id))["selected"] == worker_id
    assert sm.selected_worker_id is not None

    items = await call(by_name, "list_attention_items")
    assert items and items[0]["worker_id"] == worker_id

    await call(
        by_name, "record_decision", job_id=job["id"],
        question="Must legacy records remain writable?", answer="Read legacy only",
    )
    assert sm.store.list_decisions(sm.store.get_job(_uuid(job["id"])).id)

    assert (await call(by_name, "route_message", worker_id=worker_id, message="thanks"))["sent"]
    await settle()

    assert (await call(by_name, "interrupt_worker", worker_id=worker_id))["interrupted"]
    assert (await call(by_name, "stop_worker", worker_id=worker_id))["stopped"]
    assert sm.store.get_worker(_uuid(worker_id)).status is WorkerStatus.STOPPED


async def test_tools_refuse_bad_input_with_an_actionable_message(tools, git_repo):
    manager, by_name = tools
    sm = manager.sm
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    job = sm.create_job("Guarded", repo.id)

    bad_role = await call(by_name, "create_worker", role="wizard", title="w", prompt="hi",
                          job_id=str(job.id))
    assert "wizard" in bad_role["refused"] and "planner" in bad_role["refused"]

    bad_workflow = await call(by_name, "start_workflow", workflow_name="do-magic",
                              job_id=str(job.id))
    assert "do-magic" in bad_workflow["refused"] and "plan-feature" in bad_workflow["refused"]

    missing_plan = await call(by_name, "start_workflow",
                              workflow_name="implement-approved-plan", job_id=str(job.id))
    assert "implementation_contract" in missing_plan["refused"]

    unregistered = await call(by_name, "register_repository", path="/nowhere/at/all")
    assert "does not exist" in unregistered["refused"]

    malformed = await call(by_name, "inspect_worker", worker_id="not-a-uuid")
    assert "refused" in malformed, "a malformed id must refuse, not raise"


async def test_cleanup_through_the_manager_needs_the_users_own_confirmation(tools, git_repo):
    manager, by_name = tools
    sm = manager.sm
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    job = sm.create_job("Cleanup", repo.id)
    worker = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER, title="w", prompt="hi", job_id=job.id, writable=True
    )
    await settle()

    # The model asserting confirmed=true is not enough: the user never said so.
    manager._user_confirmed = False
    result = await call(by_name, "request_cleanup", worker_id=str(worker.id), confirmed=True)
    assert result["performed"] is False
    assert "confirmation" in result["explanation"]
    assert sm.store.get_worktree(worker.worktree_id).path.exists()

    manager._user_confirmed = True
    result = await call(by_name, "request_cleanup", worker_id=str(worker.id), confirmed=True)
    assert result["performed"] is True


async def test_a_destructive_request_is_gated_before_the_model_is_invoked(
    session_manager, git_repo
):
    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    job = sm.create_job("Cleanup", repo.id)
    worker = await sm.create_worker(
        role=WorkerRole.IMPLEMENTER, title="w", prompt="hi", job_id=job.id, writable=True
    )
    await settle()
    sm.selected_worker_id = worker.id

    manager = ModelManager(sm)
    reply = await manager.handle("Clean up that worker.")
    assert "confirm" in reply.lower()
    assert sm.store.get_worktree(worker.worktree_id).path.exists()


async def test_the_deterministic_manager_covers_the_same_operations(session_manager, git_repo):
    sm = session_manager
    sm.register_repository(git_repo("alpha"), "alpha")
    manager = DeterministicManager(sm)

    assert "ENG-421" in await manager.handle(TICKET)
    await settle()
    job = sm.store.list_jobs()[0]
    assert sm.store.list_workers(job.id)

    # A question while a job is selected reuses that job's worker, which has the context.
    assert "ask-question" in await manager.handle("Is the cache shared?")
    await settle()

    # With nothing selected it becomes a standalone read-only question worker instead.
    sm.selected_worker_id = None
    assert "Read-only question worker" in await manager.handle("Is the cache shared?")
    await settle()
    assert any(w.role is WorkerRole.QUESTION for w in sm.store.list_workers())


def _uuid(value):
    from uuid import UUID

    return UUID(value)
