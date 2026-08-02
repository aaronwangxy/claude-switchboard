"""A request split across jobs still has one answer, and evidence travels between them.

Some work is genuinely separable: diagnose in one place, fix in another, perhaps in another
repository. When Manager splits a request it links the jobs, and Switchboard -- not the
model's memory -- carries the first job's artifacts into the second's prompts.
"""

from __future__ import annotations

import json

import pytest

from switchboard.core.errors import SessionManagerError
from switchboard.domain.enums import ArtifactType
from tests.integration.test_feature_workflow import settle

ANSWER = {
    "question": "Where does the dispatcher read preferences?",
    "answer": "It reads them from the in-process cache in dispatch.py, never from storage.",
    "findings": [
        {
            "id": "F1",
            "claim": "dispatch.py:88 reads _cache directly",
            "evidence": "dispatch.py:88",
            "confidence": "confirmed",
        }
    ],
    "open_questions": [],
}


@pytest.fixture
async def two_jobs(session_manager, git_repo, backend):
    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    parent = sm.create_job("Make preferences survive a restart", repo.id, external_ref="ENG-1")
    backend.responses["question"] = lambda spec, message: (
        "Here.\n```json\n" + json.dumps(ANSWER) + "\n```"
    )
    return sm, repo, parent


async def test_a_later_job_is_handed_the_earlier_job_s_stored_evidence(two_jobs, backend):
    sm, repo, parent = two_jobs
    investigation = sm.create_job("Where are preferences read?", repo.id, parent_job_id=parent.id)
    await sm.start_run("answer-question", job_id=investigation.id, request="Where read?")
    await settle()
    assert sm.store.latest_artifact(investigation.id, ArtifactType.FINDINGS) is not None

    fix = sm.create_job(
        "Persist preferences",
        repo.id,
        parent_job_id=parent.id,
        context_job_ids=[investigation.id],
    )
    seen: dict[str, str] = {}

    def implementer(spec, message: str) -> str:
        seen.setdefault("prompt", message)
        return "Done."

    backend.responses["implementer"] = implementer
    await sm.start_workflow("implement-fix", job_id=fix.id, request="Persist them.")
    await settle()

    assert "in-process cache in dispatch.py" in seen["prompt"]
    assert "(from Where are preferences read?)" in seen["prompt"], "the source is attributed"


async def test_a_parent_is_not_complete_while_a_child_is_not(two_jobs):
    sm, repo, parent = two_jobs
    parent.composite_workflow = "answer-question"
    sm.store.save_job(parent)
    child = sm.create_job("Sub-question", repo.id, external_ref="ENG-1a", parent_job_id=parent.id)

    report = sm.job_completion(parent.id)
    assert any("ENG-1a" in blocker for blocker in report.blockers)
    assert not report.ready

    child.completed_at = child.updated_at
    sm.store.save_job(child)
    assert not any(
        "ENG-1a" in blocker for blocker in sm.job_completion(parent.id).blockers
    )


async def test_linking_to_a_job_that_does_not_exist_is_refused(two_jobs):
    from uuid import uuid4

    sm, repo, _ = two_jobs
    with pytest.raises(SessionManagerError, match="does not exist"):
        sm.create_job("Orphan", repo.id, parent_job_id=uuid4())
    with pytest.raises(SessionManagerError, match="does not exist"):
        sm.create_job("Orphan", repo.id, context_job_ids=[uuid4()])


async def test_a_linked_job_survives_a_restart(two_jobs, sb_home):
    from switchboard.storage.store import Store

    sm, repo, parent = two_jobs
    child = sm.create_job(
        "Sub", repo.id, parent_job_id=parent.id, context_job_ids=[parent.id]
    )
    sm.store.close()

    reopened = Store(sb_home / "switchboard.db")
    try:
        loaded = reopened.get_job(child.id)
        assert loaded.parent_job_id == parent.id
        assert loaded.context_job_ids == [parent.id]
    finally:
        reopened.close()
