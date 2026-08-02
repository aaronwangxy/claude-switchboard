"""One session diagnoses, the next fixes what it found, a third verifies.

This is the firefighting shape: no plan-and-approve ritual, but the fix worker must be
given the investigator's evidence rather than a summary somebody retyped. The handoff is
the stored findings artifact, so it survives a restart and cannot drift from what the
investigator actually said.
"""

from __future__ import annotations

import json

import pytest

from switchboard.domain.enums import ArtifactType, WorkerRole, WorkerStatus
from tests.conftest import commit_file
from tests.integration.test_feature_workflow import settle

FINDINGS = {
    "goal": "Identify why workers see _started before _items is populated.",
    "criteria": [
        {
            "id": "AC1",
            "statement": "The unsynchronised write ordering is named with a line",
            "established_by": "read taskq.py",
            "status": "passed",
        }
    ],
    "question": "Why is there a startup race?",
    "answer": "start() sets _started before assigning _items, and neither write is "
    "under the lock, so a reader that observes _started can still see the empty list.",
    "findings": [
        {
            "id": "F1",
            "claim": "_started is written before _items in start()",
            "evidence": "taskq.py:13-15 assigns _started then _items, outside _lock",
            "confidence": "confirmed",
        }
    ],
    "open_questions": [],
}

REVIEW_PASS = '```json\n{"verdict":"pass","findings":[]}\n```'


def verification_for(criterion: str) -> str:
    return (
        "Verified.\n```json\n"
        + json.dumps(
            {
                "scope": "full",
                "evidence": [
                    {
                        "criterion_id": criterion,
                        "status": "passed",
                        "commands": [
                            {"command": "pytest -q", "exit_code": 0, "output_excerpt": "1 passed"}
                        ],
                        "observed_behavior": "the queue is populated before it is startable",
                    }
                ],
            }
        )
        + "\n```"
    )


@pytest.fixture
async def firefight(session_manager, git_repo, backend):
    sm = session_manager
    repo = sm.register_repository(git_repo("taskq"), "taskq")
    job = sm.create_job("Startup race in the task queue", repo.id)
    seen: dict[str, str] = {}

    def investigator(spec, message: str) -> str:
        seen.setdefault("investigator", message)
        return "Found it.\n```json\n" + json.dumps(FINDINGS) + "\n```"

    def implementer(spec, message: str) -> str:
        # `finalize-change` is an implementer too, so keep the first message only.
        seen.setdefault("implementer", message)
        if not (spec.cwd / "fix.txt").exists():
            commit_file(spec.cwd, "fix.txt", "locked\n", "fix: populate before start")
        return "Fixed the ordering and added a regression test."

    backend.responses["investigator"] = investigator
    backend.responses["implementer"] = implementer
    backend.responses["verifier"] = lambda spec, message: verification_for("AC1")
    backend.responses["reviewer"] = lambda spec, message: REVIEW_PASS
    return sm, job, seen


async def test_the_fix_worker_is_given_the_investigator_s_evidence(firefight):
    sm, job, seen = firefight
    await sm.start_run("diagnose-and-fix", job_id=job.id, request="Workers see an empty queue.")
    await settle()

    assert "investigator" in seen, "the first session diagnoses"
    assert "implementer" in seen, "a second session fixes what it found"
    handed_over = seen["implementer"]
    assert "start() sets _started before assigning _items" in handed_over
    assert "taskq.py:13-15" in handed_over, "the evidence travels, not just the conclusion"


async def test_the_findings_are_durable_not_a_transcript(firefight):
    sm, job, _ = firefight
    await sm.start_run("diagnose-and-fix", job_id=job.id, request="Workers see an empty queue.")
    await settle()

    artifact = sm.store.latest_artifact(job.id, ArtifactType.FINDINGS)
    assert artifact is not None
    assert "neither write is under the lock" in artifact.body["answer"]
    assert artifact.body["findings"][0]["confidence"] == "confirmed"


async def test_each_stage_is_its_own_independent_session(firefight):
    sm, job, _ = firefight
    await sm.start_run("diagnose-and-fix", job_id=job.id, request="Workers see an empty queue.")
    await settle()

    workers = sm.store.list_workers(job.id)
    roles = [w.role for w in workers]
    assert WorkerRole("investigator") in roles
    assert WorkerRole.IMPLEMENTER in roles
    assert WorkerRole.VERIFIER in roles
    assert len({w.id for w in workers}) == len(workers)
    # Only the fixer may write; diagnosis and verification observe.
    assert [w.role for w in workers if w.writable] == [WorkerRole.IMPLEMENTER]


async def test_a_firefight_reaches_a_deterministic_completion(firefight):
    sm, job, _ = firefight
    await sm.start_run("diagnose-and-fix", job_id=job.id, request="Workers see an empty queue.")
    await settle()

    report = sm.job_completion(job.id)
    assert report.workflow == "diagnose-and-fix"
    # Its definition of done is findings plus evidence -- never an approved plan, which
    # this workflow deliberately does not produce.
    assert "implementation_contract" not in report.required
    assert set(report.required) >= {"findings", "goal", "verification", "review"}
    assert report.ready, report.blockers
    assert sm.store.get_job(job.id).completed_at is not None


async def test_a_repeated_step_does_not_leave_its_previous_session_running(
    session_manager, git_repo, backend
):
    """A `fresh` step that runs twice used to leave the first session alive and idle.

    Live, a `diagnose-and-fix` job ended with three verifiers on one worktree, each
    holding a report a later commit had already invalidated. Only the newest attempt at a
    step survives; the other phases keep the session the user can still drop into.
    """
    sm = session_manager
    repo = sm.register_repository(git_repo("taskq"), "taskq")
    job = sm.create_job("Startup race in the task queue", repo.id)
    fixes = iter(("fix.txt", "fix2.txt"))
    reviews = iter(
        (
            '```json\n{"verdict":"changes_requested","findings":[{"id":"F1",'
            '"severity":"blocking","category":"correctness",'
            '"description":"The fix publishes _items outside the lock.",'
            '"location":"taskq.py:13"}]}\n```',
            REVIEW_PASS,
        )
    )

    def implementer(spec, message: str) -> str:
        name = next(fixes, None)
        if name is not None:
            commit_file(spec.cwd, name, "locked\n", f"fix: {name}")
        return "Fixed."

    backend.responses["investigator"] = lambda spec, m: (
        "Found it.\n```json\n" + json.dumps(FINDINGS) + "\n```"
    )
    backend.responses["implementer"] = implementer
    backend.responses["verifier"] = lambda spec, m: verification_for("AC1")
    backend.responses["reviewer"] = lambda spec, m: next(reviews, REVIEW_PASS)

    await sm.start_run("diagnose-and-fix", job_id=job.id, request="Workers see an empty queue.")
    await settle()

    workers = sm.store.list_workers(job.id)
    verifiers = [w for w in workers if w.role is WorkerRole.VERIFIER]
    assert len(verifiers) > 1, "the review sent the run back through verification"
    live = [w for w in verifiers if w.status is not WorkerStatus.STOPPED]
    assert len(live) == 1, f"only the newest verifier stays running, got {[w.status for w in live]}"
    assert live[0].created_at == max(w.created_at for w in verifiers)
    # The investigator is a different role, so it is still there to be asked a follow-up.
    investigators = [w for w in workers if w.role == WorkerRole("investigator")]
    assert [w.status for w in investigators] == [WorkerStatus.IDLE]


async def test_a_writable_session_is_never_retired_automatically(firefight):
    """A writable session owns a worktree and *is* the job's change, so it is kept.

    Retirement tidies up observers whose reports a later commit invalidated. Ending a
    session that holds uncommitted work, or that a later `existing` step is going to
    resume, would be Switchboard discarding work rather than managing sessions.
    """
    sm, job, _ = firefight
    await sm.start_run("diagnose-and-fix", job_id=job.id, request="Workers see an empty queue.")
    await settle()

    writable = [w for w in sm.store.list_workers(job.id) if w.writable]
    assert writable, "the fix step ran"
    assert all(w.status is not WorkerStatus.STOPPED for w in writable)


async def test_a_fix_cannot_start_before_something_was_actually_diagnosed(
    session_manager, git_repo
):
    """`implement-fix` requires findings, so the ordering is enforced, not merely hoped for."""
    sm = session_manager
    repo = sm.register_repository(git_repo("taskq"), "taskq")
    job = sm.create_job("Startup race", repo.id)

    with pytest.raises(Exception, match="findings"):
        await sm.start_workflow("implement-fix", job_id=job.id, request="just fix it")
