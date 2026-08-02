"""One session diagnoses, the next fixes what it found, a third verifies.

This is the firefighting shape: no plan-and-approve ritual, but the fix worker must be
given the investigator's evidence rather than a summary somebody retyped. The handoff is
the stored findings artifact, so it survives a restart and cannot drift from what the
investigator actually said.
"""

from __future__ import annotations

import json

import pytest

from switchboard.domain.enums import ArtifactType, WorkerRole
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


async def test_a_fix_cannot_start_before_something_was_actually_diagnosed(
    session_manager, git_repo
):
    """`implement-fix` requires findings, so the ordering is enforced, not merely hoped for."""
    sm = session_manager
    repo = sm.register_repository(git_repo("taskq"), "taskq")
    job = sm.create_job("Startup race", repo.id)

    with pytest.raises(Exception, match="findings"):
        await sm.start_workflow("implement-fix", job_id=job.id, request="just fix it")
