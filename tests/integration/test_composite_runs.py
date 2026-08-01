"""Composite workflow runs: the development ritual as configurable, reproducible state."""

from __future__ import annotations

from pathlib import Path

import pytest

from csm.agents.manager import DeterministicManager
from csm.core.session_manager import SessionManagerError
from csm.domain.enums import ArtifactType, JobStage, RunStatus, WorkerRole
from csm.workflows.registry import WorkflowError, reload_workflows
from tests.conftest import TICKET
from tests.integration.test_feature_workflow import committing_responder, settle


@pytest.fixture
async def project(session_manager, git_repo, backend):
    repo_path = git_repo("alpha")
    repo = session_manager.register_repository(repo_path, "alpha")
    backend.responses["implementer"] = committing_responder(
        "preferences.py", "PREFERENCES = {}\n", "feat: add preferences"
    )
    return session_manager, backend, repo


BLOCKING_REVIEW = (
    "Changes requested.\n\n```json\n"
    '{"verdict": "changes_requested", "findings": [{"id": "F1", "severity": "blocking",'
    ' "category": "correctness", "description": "Preferences are never read back.",'
    ' "evidence": "no lookup", "recommended_action": "Read them."}]}\n```'
)


async def paste_ticket(sm) -> tuple:
    manager = DeterministicManager(sm)
    reply = await manager.handle(TICKET)
    await settle()
    job = sm.store.list_jobs()[0]
    return manager, job, reply


# ------------------------------------------------------------------ starting


async def test_a_pasted_ticket_starts_the_complete_ticket_run_on_its_first_step(project):
    sm, backend, repo = project
    _, job, reply = await paste_ticket(sm)

    run = sm.store.active_run(job.id)
    assert run is not None
    assert run.workflow == "complete-ticket"
    assert run.step_index == 0, "the run is on plan-feature"
    assert "complete-ticket" in reply

    (planner,) = sm.store.list_workers(job.id)
    assert planner.role is WorkerRole.PLANNER
    assert planner.writable is False
    assert planner.workflow == "plan-feature"


async def test_the_run_pauses_for_the_user_and_does_not_implement_unapproved_work(project):
    sm, backend, repo = project
    _, job, _ = await paste_ticket(sm)

    run = sm.store.active_run(job.id)
    assert run.status is RunStatus.AWAITING_APPROVAL
    assert [w.role for w in sm.store.list_workers(job.id)] == [WorkerRole.PLANNER]


async def test_the_job_records_the_profile_it_is_following(project):
    sm, backend, repo = project
    _, job, _ = await paste_ticket(sm)
    assert sm.store.get_job(job.id).profile == "complete-ticket"


# ------------------------------------------------------------------ advancing


async def approve_and_run(sm, job) -> None:
    sm.record_decision(job.id, "Must legacy records remain writable?", "Read legacy, write new only")
    sm.approve_plan(job.id)
    await settle()


async def test_approving_the_plan_carries_the_job_through_the_whole_ritual(project):
    sm, backend, repo = project
    _, job, _ = await paste_ticket(sm)
    await approve_and_run(sm, job)

    roles = [w.role for w in sm.store.list_workers(job.id)]
    assert WorkerRole.IMPLEMENTER in roles
    assert WorkerRole.VERIFIER in roles
    assert WorkerRole.REVIEWER in roles

    assert sm.store.latest_artifact(job.id, ArtifactType.VERIFICATION) is not None
    assert sm.store.latest_artifact(job.id, ArtifactType.REVIEW) is not None
    assert sm.store.active_run(job.id) is None, "the run finished"
    assert sm.store.list_runs(job.id)[-1].status is RunStatus.COMPLETED
    assert sm.ready_to_push(job.id).ready
    assert sm.store.get_job(job.id).stage is JobStage.READY_TO_PUSH


async def test_each_step_runs_on_the_worker_its_declaration_asks_for(project):
    sm, backend, repo = project
    _, job, _ = await paste_ticket(sm)
    await approve_and_run(sm, job)

    workers = sm.store.list_workers(job.id)
    reviewers = [w for w in workers if w.role is WorkerRole.REVIEWER]
    implementers = [w for w in workers if w.role is WorkerRole.IMPLEMENTER]
    assert len(reviewers) == 1, "the review step gets its own fresh session"
    assert len(implementers) == 1, "finalize reuses the implementer rather than forking one"
    assert reviewers[0].writable is False
    assert implementers[0].writable is True


async def test_a_conditional_step_is_skipped_when_its_condition_does_not_hold(project):
    sm, backend, repo = project
    _, job, _ = await paste_ticket(sm)
    await approve_and_run(sm, job)

    executions = [e.workflow for e in sm.store.list_workflow_executions(job.id)]
    assert executions.count("full-verify") == 1, "re-verify only when the code moved"
    assert executions.count("independent-review") == 1
    assert "address-review-comments" not in executions, "nothing to fix, so no fix step"


async def test_blocking_findings_trigger_the_bounded_fix_loop(project):
    sm, backend, repo = project
    backend.responses["reviewer"] = lambda spec, message: BLOCKING_REVIEW
    _, job, _ = await paste_ticket(sm)
    await approve_and_run(sm, job)

    executions = [e.workflow for e in sm.store.list_workflow_executions(job.id)]
    assert "address-review-comments" in executions, "a blocking finding starts the fix step"
    assert executions.count("address-review-comments") <= 2, "the repeat is bounded"

    run = sm.store.list_runs(job.id)[-1]
    assert run.status is RunStatus.COMPLETED, "a bounded loop terminates rather than spinning"
    assert max(run.iterations.values()) <= 2
    assert not sm.ready_to_push(job.id).ready
    assert sm.store.get_job(job.id).stage is JobStage.FIXING


# -------------------------------------------------------------- profile choice


async def test_a_repository_preference_selects_a_different_profile(project):
    sm, backend, repo = project
    sm.set_repository_profile(repo.id, "lightweight-feature")
    _, job, _ = await paste_ticket(sm)
    await approve_and_run(sm, job)

    assert sm.store.get_job(job.id).profile == "lightweight-feature"
    executions = [e.workflow for e in sm.store.list_workflow_executions(job.id)]
    assert "smoke-test" in executions
    assert "independent-review" not in executions, "this profile has no review step"


async def test_an_unknown_profile_is_refused_before_it_is_stored(project):
    sm, backend, repo = project
    with pytest.raises(WorkflowError):
        sm.set_repository_profile(repo.id, "no-such-profile")
    assert sm.resolve_profile(repo.id) == "complete-ticket"


CUSTOM_PROFILE = """\
name: plan-and-smoke
description: Plan, implement, and smoke test. Nothing else.
steps:
  - workflow: plan-feature
    approval: required
  - workflow: implement-approved-plan
  - workflow: smoke-test
    worker: fresh
"""


async def test_a_user_defined_profile_drives_a_real_job(project, isolated_workflows: Path):
    sm, backend, repo = project
    isolated_workflows.mkdir(parents=True, exist_ok=True)
    (isolated_workflows / "plan-and-smoke.yaml").write_text(CUSTOM_PROFILE)
    assert reload_workflows() == []
    sm.set_repository_profile(repo.id, "plan-and-smoke")

    _, job, _ = await paste_ticket(sm)
    await approve_and_run(sm, job)

    executions = [e.workflow for e in sm.store.list_workflow_executions(job.id)]
    assert executions == ["plan-feature", "implement-approved-plan", "smoke-test"]
    assert sm.store.latest_artifact(job.id, ArtifactType.SMOKE_VERIFICATION) is not None
    assert sm.store.list_runs(job.id)[-1].status is RunStatus.COMPLETED


# ------------------------------------------------------------- reproducibility


async def test_run_state_is_durable_and_readable_without_any_transcript(project):
    sm, backend, repo = project
    _, job, _ = await paste_ticket(sm)

    reloaded = sm.store.get_run(sm.store.active_run(job.id).id)
    assert reloaded.workflow == "complete-ticket"
    assert reloaded.status is RunStatus.AWAITING_APPROVAL
    assert reloaded.detail, "a paused run says why in one sentence"
    assert reloaded.request.startswith("ENG-421")


async def test_two_runs_cannot_race_on_one_job(project):
    sm, backend, repo = project
    _, job, _ = await paste_ticket(sm)
    with pytest.raises(SessionManagerError) as excinfo:
        await sm.start_run("complete-ticket", job_id=job.id)
    assert "already running" in str(excinfo.value)
