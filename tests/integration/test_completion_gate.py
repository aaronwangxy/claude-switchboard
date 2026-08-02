"""What "finished" means comes from the workflow, not from one workflow's checklist.

The old gate asked every job for an approved implementation contract, acceptance criteria,
verification and an independent review. That is `complete-ticket`'s definition of done, so
a job following any other workflow could never be reported complete. These tests pin both
halves of the fix: the old behaviour survives for `complete-ticket`, and a workflow that
promises something else is judged against what it promised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from switchboard.core import evidence
from switchboard.domain.enums import ArtifactType, AttentionKind
from switchboard.domain.models import Artifact
from switchboard.workflows.registry import get_workflow, reload_workflows
from tests.conftest import commit_file
from tests.integration.test_feature_workflow import settle


@pytest.fixture
def job(session_manager, git_repo):
    repo = session_manager.register_repository(git_repo("alpha"), "alpha")
    return session_manager, session_manager.create_job("Notification preferences", repo.id)


def _write(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(body)


# ------------------------------------------------- what a workflow asks for


def test_complete_ticket_still_demands_exactly_what_it_always_did():
    """The generalisation must not quietly relax the ritual it was derived from."""
    required = evidence.required_artifacts(get_workflow("complete-ticket"))
    assert required == frozenset(
        {
            ArtifactType.IMPLEMENTATION_CONTRACT,
            ArtifactType.BEHAVIOR_CONTRACT,
            ArtifactType.VERIFICATION,
            ArtifactType.REVIEW,
        }
    )
    assert evidence.touches_code(get_workflow("complete-ticket"))


def test_a_conditional_step_is_never_a_precondition_for_done():
    """`address-review-comments` only runs when there are findings.

    Requiring its artifact would leave a clean change permanently unfinished.
    """
    definition = get_workflow("complete-ticket")
    assert any(step.workflow == "address-review-comments" for step in definition.steps)
    assert ArtifactType.COMMENT_RESOLUTIONS not in evidence.required_artifacts(definition)


def test_a_lighter_workflow_is_not_asked_for_a_review_it_never_runs():
    required = evidence.required_artifacts(get_workflow("lightweight-feature"))
    assert ArtifactType.SMOKE_VERIFICATION in required
    assert ArtifactType.REVIEW not in required, "it deliberately has no review step"


def test_a_user_workflow_defines_its_own_done(isolated_workflows: Path):
    _write(
        isolated_workflows,
        "just-verify.yaml",
        "name: just-verify\nsteps:\n  - workflow: full-verify\n",
    )
    assert reload_workflows() == []
    required = evidence.required_artifacts(get_workflow("just-verify"))
    assert required == frozenset({ArtifactType.VERIFICATION})
    assert not evidence.touches_code(get_workflow("just-verify"))


# ------------------------------------------------------------- the live gate


async def test_a_job_following_no_workflow_is_never_announced_complete(job):
    """No workflow means nothing declared what done is; saying so would be an opinion."""
    sm, created = job
    report = sm.job_completion(created.id)
    assert report.workflow is None
    assert not report.ready, "an empty checklist is not a satisfied one"
    assert "not following a workflow" in report.blockers[0]


async def test_a_verification_only_workflow_completes_without_a_plan_or_a_review(
    session_manager, git_repo, backend, isolated_workflows
):
    """The point of the whole change: a peer workflow can actually finish."""
    _write(
        isolated_workflows,
        "just-verify.yaml",
        "name: just-verify\nsteps:\n  - workflow: full-verify\n",
    )
    sm = session_manager
    sm.reload_workflows()
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    created = sm.create_job("Confirm the fix still holds", repo.id)

    # `full-verify` requires a behavior contract, so give the job one and its criteria.
    sm.store.save_artifact(
        Artifact(
            job_id=created.id,
            type=ArtifactType.BEHAVIOR_CONTRACT,
            body={
                "criteria": [
                    {
                        "id": "AC1",
                        "behavior": "it boots",
                        "verification_method": "run it",
                        "status": "passed",
                    }
                ]
            },
        )
    )
    backend.responses["verifier"] = lambda spec, message: (
        '```json\n{"scope":"full","evidence":[{"criterion_id":"AC1","status":"passed",'
        '"commands":[{"command":"pytest","exit_code":0,"output_excerpt":"ok"}],'
        '"observed_behavior":"it boots"}]}\n```'
    )

    run = await sm.start_run("just-verify", job_id=created.id)
    await settle()
    report = sm.job_completion(created.id)
    assert report.workflow == "just-verify"
    assert ArtifactType.VERIFICATION.value in report.required
    assert "implementation_contract" not in report.required
    assert report.ready, report.blockers
    assert sm.store.get_job(created.id).completed_at is not None
    assert any(
        item.kind is AttentionKind.WORK_COMPLETE for item in sm.store.list_attention_items()
    ), "the user is told when the requested work is actually done"
    assert run.workflow == "just-verify"


async def test_an_unfinished_run_is_itself_a_blocker(session_manager, git_repo):
    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    created = sm.create_job("ENG-9", repo.id)
    created.composite_workflow = "complete-ticket"
    sm.store.save_job(created)

    report = sm.job_completion(created.id)
    assert not report.ready
    assert any("No implementation contract" in blocker for blocker in report.blockers)
    assert report.workflow == "complete-ticket"


async def test_a_dirty_authoritative_tree_blocks_completion(session_manager, git_repo, backend):
    sm = session_manager
    repo = sm.register_repository(git_repo("alpha"), "alpha")
    created = sm.create_job("ENG-9", repo.id)
    created.composite_workflow = "complete-ticket"
    sm.store.save_job(created)
    worker = await sm.create_worker(
        role=get_workflow("implement-approved-plan").role,
        title="impl",
        prompt="",
        job_id=created.id,
        writable=True,
    )
    worktree = sm.store.get_worktree(worker.worktree_id)
    commit_file(worktree.path, "a.py", "x = 1\n", "feat: a")
    (worktree.path / "dirty.py").write_text("half done\n")

    report = sm.job_completion(created.id)
    assert any("uncommitted" in blocker for blocker in report.blockers)
