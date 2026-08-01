"""The whole feature loop, plan through ready-to-push, over real git repositories."""

from __future__ import annotations

import asyncio

import pytest

from switchboard.agents.manager import DeterministicManager
from switchboard.domain.enums import ArtifactType, JobStage, WorkerRole, WorkerStatus
from switchboard.gitops import runner
from tests.conftest import TICKET, commit_file


async def settle() -> None:
    """Let the event pumps drain and any composite run reach its next pause.

    A composite step chains through several tasks (pump -> advance -> new worker -> pump),
    so this gives the loop several full rounds rather than one.
    """
    for _ in range(10):
        for _ in range(50):
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)


def committing_responder(name: str, content: str, message: str):
    """An implementer that commits once. Later turns are a no-op, as a real one would be."""

    def respond(spec, _message: str) -> str:
        path = spec.cwd / name
        if path.exists() and path.read_text() == content:
            return f"Nothing further to commit for {message!r}."
        commit_file(spec.cwd, name, content, message)
        return f"Committed {message!r}."

    return respond


@pytest.fixture
async def project(session_manager, git_repo, backend):
    repo_path = git_repo("alpha")
    repo = session_manager.register_repository(repo_path, "alpha")
    return session_manager, backend, repo


async def drive_to_review(sm, backend, repo) -> tuple:
    """Paste a ticket and let the complete-ticket run carry it through review.

    Nothing here names a workflow: pasting the ticket starts the composite, and answering
    the decision plus approving the plan is the only human input the ritual needs.
    """
    manager = DeterministicManager(sm)
    backend.responses["implementer"] = committing_responder(
        "preferences.py", "PREFERENCES = {}\n", "feat: add preferences"
    )
    await manager.handle(TICKET)
    await settle()
    job = sm.store.list_jobs()[0]

    sm.record_decision(job.id, "Must legacy records remain writable?", "Read legacy, write new only")
    sm.approve_plan(job.id)
    await settle()

    impl = next(w for w in sm.store.list_workers(job.id) if w.role is WorkerRole.IMPLEMENTER)
    return manager, job, impl


# --------------------------------------------------------------- ticket intake


async def test_pasted_ticket_creates_a_job_and_a_read_only_planner(project):
    sm, backend, repo = project
    manager = DeterministicManager(sm)
    reply = await manager.handle(TICKET)
    await settle()

    jobs = sm.store.list_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.external_ref == "ENG-421"
    assert job.stage is JobStage.PLANNING
    assert "ENG-421" in reply

    (planner,) = sm.store.list_workers(job.id)
    assert planner.role is WorkerRole.PLANNER
    assert planner.writable is False
    assert planner.worktree_id is None, "a read-only planner needs no worktree"
    assert planner.session_id, "every worker captures its own session id"


async def test_plan_produces_all_three_contracts(project):
    sm, backend, repo = project
    await DeterministicManager(sm).handle(TICKET)
    await settle()
    job = sm.store.list_jobs()[0]

    contract = sm.store.latest_artifact(job.id, ArtifactType.IMPLEMENTATION_CONTRACT)
    assert contract is not None
    assert len(contract.body["summary_lines"]) <= 10
    assert contract.body["commit_stack"], "the plan proposes an atomic commit stack"
    assert contract.body["risks"]

    decision = contract.body["decisions"][0]
    assert decision["options"] and decision["recommendation"], "decisions offer options and advice"
    assert decision["blocking"] is True

    behavior = sm.store.latest_artifact(job.id, ArtifactType.BEHAVIOR_CONTRACT)
    criterion = behavior.body["criteria"][0]
    assert criterion["verification_method"] and criterion["evidence_required"]


async def test_blocking_plan_raises_a_prioritised_attention_item(project):
    sm, backend, repo = project
    await DeterministicManager(sm).handle(TICKET)
    await settle()
    (planner,) = sm.store.list_workers()
    assert planner.status is WorkerStatus.BLOCKED
    assert planner.waiting_for

    items = sm.list_attention_items()
    assert items and items[0].worker_id == planner.id
    assert items[0].reason


async def test_approving_the_plan_clears_it_from_the_attention_queue(project):
    sm, backend, repo = project
    await DeterministicManager(sm).handle(TICKET)
    await settle()
    job = sm.store.list_jobs()[0]
    assert any(i.kind.value == "plan_approval" for i in sm.list_attention_items())

    sm.record_decision(job.id, "Must legacy records remain writable?", "Read legacy only")
    sm.approve_plan(job.id)
    assert not any(i.kind.value == "plan_approval" for i in sm.list_attention_items())


async def test_a_plan_cannot_be_approved_while_a_decision_blocks_it(project):
    sm, backend, repo = project
    await DeterministicManager(sm).handle(TICKET)
    await settle()
    job = sm.store.list_jobs()[0]
    with pytest.raises(Exception, match="blocking decision"):
        sm.approve_plan(job.id)
    sm.record_decision(job.id, "Must legacy records remain writable?", "Read legacy only")
    sm.approve_plan(job.id)


async def test_implementation_cannot_start_without_an_approved_plan(project):
    """The declared prerequisites are enforced in code, not by asking a model nicely."""
    sm, backend, repo = project
    manager = DeterministicManager(sm)

    job = sm.create_job("No plan yet", sm.store.list_repositories()[0].id)
    with pytest.raises(Exception, match="needs a current implementation_contract"):
        await sm.start_workflow("implement-approved-plan", job_id=job.id, request="go")

    await manager.handle(TICKET)
    await settle()
    planned = sm.store.list_jobs()[0]
    with pytest.raises(Exception, match="has not been approved"):
        await sm.start_workflow("implement-approved-plan", job_id=planned.id, request="go")

    sm.record_decision(planned.id, "Must legacy records remain writable?", "Read legacy only")
    sm.approve_plan(planned.id)
    worker = await sm.start_workflow("implement-approved-plan", job_id=planned.id, request="go")
    assert worker.writable


async def test_verification_cannot_run_without_a_behavior_contract(project):
    sm, backend, repo = project
    job = sm.create_job("No criteria", sm.store.list_repositories()[0].id)
    with pytest.raises(Exception, match="needs a current behavior_contract"):
        await sm.start_workflow("full-verify", job_id=job.id)


# ------------------------------------------------------------ the full loop


async def test_full_feature_loop_reaches_ready_to_push_with_a_blurb(project):
    sm, backend, repo = project
    manager, job, impl = await drive_to_review(sm, backend, repo)

    impl = sm.store.get_worker(impl.id)
    assert impl.writable and impl.worktree_id, "implementation happens in its own worktree"

    verification = sm.store.latest_artifact(job.id, ArtifactType.VERIFICATION)
    head = runner.head_commit(sm.store.get_worktree(impl.worktree_id).path)
    assert verification.head_commit == head, "evidence is tied to the exact tested head"
    assert verification.body["evidence"][0]["commands"][0]["exit_code"] == 0

    review = sm.store.latest_artifact(job.id, ArtifactType.REVIEW)
    assert review.head_commit == head
    assert review.body["verdict"] == "pass"

    report = sm.ready_to_push(job.id)
    assert report.ready, report.blockers
    assert "Verification performed:" in report.blurb
    assert "Limitations:" in report.blurb
    assert "AC1" in report.blurb


async def test_the_implementer_is_seeded_with_contracts_not_the_planner_transcript(project):
    sm, backend, repo = project
    manager, job, impl = await drive_to_review(sm, backend, repo)

    seeded = next(s for s in backend.started if s.worker_id == impl.id)
    assert "summary_lines" in seeded.initial_prompt, "the approved contract is passed through"
    assert "Read legacy, write new only" in seeded.initial_prompt, "so are recorded decisions"

    planner = next(w for w in sm.store.list_workers(job.id) if w.role is WorkerRole.PLANNER)
    planner_messages = [m.text for m in sm.store.transcript(planner.id) if m.role == "assistant"]
    assert planner_messages
    assert not any(text in seeded.initial_prompt for text in planner_messages)


async def test_the_reviewer_gets_the_diff_and_evidence_but_no_implementer_reasoning(project):
    sm, backend, repo = project
    manager, job, impl = await drive_to_review(sm, backend, repo)

    reviewer = next(w for w in sm.store.list_workers(job.id) if w.role is WorkerRole.REVIEWER)
    seeded = next(s for s in backend.started if s.worker_id == reviewer.id)
    assert "preferences.py" in seeded.initial_prompt, "the reviewer sees the real diff"
    assert "feat: add preferences" in seeded.initial_prompt, "and the commit stack"
    assert "observed_behavior" in seeded.initial_prompt, "and the verification evidence"
    assert reviewer.writable is False

    impl_messages = [m.text for m in sm.store.transcript(impl.id) if m.role == "assistant"]
    assert not any(text in seeded.initial_prompt for text in impl_messages)


async def test_a_code_change_invalidates_verification_and_review_and_blocks_the_push(project):
    sm, backend, repo = project
    manager, job, impl = await drive_to_review(sm, backend, repo)
    assert sm.ready_to_push(job.id).ready

    backend.responses["implementer"] = committing_responder(
        "preferences.py", "PREFERENCES = {'email': True}\n", "fix: honour email channel"
    )
    await sm.send(impl.id, "Also handle the email channel.")
    await settle()

    verification = sm.store.latest_artifact(job.id, ArtifactType.VERIFICATION)
    review = sm.store.latest_artifact(job.id, ArtifactType.REVIEW)
    assert verification.stale and "implementation_edit" in verification.stale_reason
    assert review.stale

    report = sm.ready_to_push(job.id)
    assert not report.ready
    assert any("current HEAD" in blocker for blocker in report.blockers)


async def test_a_blocking_review_finding_prevents_ready_to_push(project):
    sm, backend, repo = project

    def blocking_review(spec, message):
        return (
            "Changes requested.\n\n```json\n"
            '{"verdict": "changes_requested", "findings": [{"id": "F1", "severity": "blocking",'
            ' "category": "correctness", "description": "Preferences are never read back.",'
            ' "evidence": "dispatcher.py has no lookup", "recommended_action": "Read them."}]}'
            "\n```"
        )

    backend.responses["reviewer"] = blocking_review
    manager, job, impl = await drive_to_review(sm, backend, repo)

    report = sm.ready_to_push(job.id)
    assert not report.ready
    assert any("blocking review finding" in blocker for blocker in report.blockers)
    assert sm.store.get_job(job.id).stage is JobStage.FIXING
    assert any(i.kind.value == "blocking_review_finding" for i in sm.list_attention_items())


async def test_verification_failure_is_recorded_honestly_and_blocks_the_push(project):
    sm, backend, repo = project

    def failing_verify(spec, message):
        return (
            "Fail. AC1 could not be verified.\n\n```json\n"
            '{"scope": "full", "evidence": [{"criterion_id": "AC1", "status": "failed",'
            ' "commands": [{"command": "pytest -q", "exit_code": 1, "output_excerpt": "1 failed"}],'
            ' "observed_behavior": "Preferences were empty after restart.", "artifacts": [],'
            ' "limitations": []}]}\n```'
        )

    backend.responses["verifier"] = failing_verify
    manager, job, impl = await drive_to_review(sm, backend, repo)

    report = sm.ready_to_push(job.id)
    assert not report.ready
    assert any("AC1" in blocker for blocker in report.blockers)
    assert any(i.kind.value == "verification_failed" for i in sm.list_attention_items())


# ---------------------------------------------------------- other workflows


async def test_smoke_test_and_rereview_can_be_invoked_independently(project):
    sm, backend, repo = project
    manager, job, impl = await drive_to_review(sm, backend, repo)
    sm.selected_worker_id = impl.id

    await manager.handle("Run another smoke test.")
    await settle()
    assert sm.store.latest_artifact(job.id, ArtifactType.SMOKE_VERIFICATION) is not None

    reviewers_before = [w for w in sm.store.list_workers(job.id) if w.role is WorkerRole.REVIEWER]
    await manager.handle("Rereview it.")
    await settle()
    reviewers_after = [w for w in sm.store.list_workers(job.id) if w.role is WorkerRole.REVIEWER]
    assert len(reviewers_after) == len(reviewers_before) + 1, "rereview starts a fresh reviewer"


async def test_address_review_comments_classifies_every_comment(project):
    sm, backend, repo = project
    manager, job, impl = await drive_to_review(sm, backend, repo)
    sm.selected_worker_id = impl.id

    def resolver(spec, message):
        return (
            "Two comments handled.\n\n```json\n"
            '{"resolutions": ['
            '{"original_comment": "cache is shared", "classification": "valid",'
            ' "reasoning": "It is process-global.", "action_taken": "Scoped per request.",'
            ' "commit": "abc123", "verification_required": ["AC1"]},'
            '{"original_comment": "rename the module", "classification": "invalid",'
            ' "reasoning": "The name matches the ticket.", "action_taken": null,'
            ' "commit": null, "verification_required": []}]}\n```'
        )

    backend.responses["implementer"] = resolver
    await manager.handle("Address these review comments: cache is shared; rename the module.")
    await settle()

    artifact = sm.store.latest_artifact(job.id, ArtifactType.COMMENT_RESOLUTIONS)
    assert artifact is not None
    resolutions = artifact.body["resolutions"]
    assert len(resolutions) == 2
    assert {r["classification"] for r in resolutions} == {"valid", "invalid"}
    assert all(r["reasoning"] for r in resolutions), "every comment gets a reason"


async def test_a_question_runs_read_only_with_no_worktree(project):
    sm, backend, repo = project
    manager = DeterministicManager(sm)
    await manager.handle("Is this cache shared between requests?")
    await settle()

    (worker,) = sm.store.list_workers()
    assert worker.role is WorkerRole.QUESTION
    assert worker.writable is False
    assert worker.worktree_id is None
    assert worker.cwd == repo.root_path
    assert sm.store.list_worktrees() == []


async def test_rebase_uses_the_configured_preferences_and_forbids_force_push(project):
    sm, backend, repo = project
    manager, job, impl = await drive_to_review(sm, backend, repo)
    sm.selected_worker_id = impl.id

    backend.responses["implementer"] = lambda spec, msg: "Rebased cleanly onto main."
    await manager.handle("Rebase this stack.")
    await settle()

    prompt = [m.text for m in sm.store.transcript(impl.id) if m.role == "user"][-1]
    assert "autosquash_fixups=True" in prompt
    assert "never_force_push=True" in prompt
    assert "Do not\nforce-push" in prompt or "not force-push" in prompt
