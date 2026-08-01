"""Mining repeated rituals into proposed workflows.

The point of mining is that CSM already records what the user actually does. The
constraint that makes it safe is that a proposal changes nothing until it is accepted.
"""

from __future__ import annotations

import json

import pytest
import yaml

from csm.core.session_manager import SessionManagerError
from csm.domain import events as ev
from csm.domain.enums import ArtifactType, WorkerRole
from csm.domain.models import WorkflowExecution
from csm.workflows.registry import get_workflow, workflow_names

PROPOSAL = {
    "proposals": [
        {
            "name": "post-rebase-verify",
            "description": "Rebase, then re-verify, because the evidence is no longer trusted.",
            "steps": [
                {"workflow": "rebase-stack", "when": "always"},
                {"workflow": "full-verify", "when": "always"},
                {"workflow": "smoke-test", "when": "always"},
            ],
            "worker": "auto",
            "evidence": "ENG-1 on Monday, ENG-2 on Tuesday, ENG-3 on Thursday.",
            "rationale": "The user assembles this by hand after every rebase.",
        }
    ]
}


@pytest.fixture
def job(session_manager, git_repo):
    repo = session_manager.register_repository(git_repo("mined"))
    return session_manager.create_job("ENG-1 Auth", repo.id, external_ref="ENG-1")


@pytest.fixture
async def miner(session_manager, job):
    return await session_manager.create_worker(
        role=WorkerRole.QUESTION,
        title="Mine workflows",
        prompt="",
        job_id=job.id,
        repository_id=job.repository_id,
        writable=False,
        workflow="mine-workflows",
    )


class TestTheMiningWorkflow:
    def test_it_is_registered_and_read_only(self):
        definition = get_workflow("mine-workflows")
        assert not definition.mutates_code
        assert ArtifactType.WORKFLOW_PROPOSALS in definition.produces

    def test_it_is_reachable_by_its_aliases(self):
        assert get_workflow("propose-workflows").name == "mine-workflows"
        assert get_workflow("find-rituals").name == "mine-workflows"


class TestHistory:
    def test_an_empty_installation_says_so(self, session_manager):
        assert "no workflow history" in session_manager.workflow_history()

    def test_it_reports_what_ran_in_order_under_each_job(self, session_manager, job):
        for name in ("rebase-stack", "full-verify", "smoke-test"):
            session_manager.store.add_workflow_execution(
                WorkflowExecution(job_id=job.id, worker_id=job.id, workflow=name)
            )
        history = session_manager.workflow_history()
        assert "ENG-1" in history
        assert history.index("rebase-stack") < history.index("full-verify") < history.index("smoke-test")

    def test_it_carries_no_repository_content(self, session_manager, job):
        session_manager.store.add_workflow_execution(
            WorkflowExecution(job_id=job.id, worker_id=job.id, workflow="full-verify")
        )
        history = session_manager.workflow_history()
        assert "diff" not in history.lower()

    def test_it_is_bounded(self, session_manager, job):
        for _ in range(30):
            session_manager.store.add_workflow_execution(
                WorkflowExecution(job_id=job.id, worker_id=job.id, workflow="full-verify")
            )
        assert session_manager.workflow_history(limit=5).count("full-verify") == 5


class TestHarvestingProposals:
    def test_a_mining_turn_stores_its_proposals(self, session_manager, job, miner):
        session_manager._finish_turn(miner, f"Found one.\n```json\n{json.dumps(PROPOSAL)}\n```")
        proposals = session_manager.list_proposals(job.id)
        assert [p.name for p in proposals] == ["post-rebase-verify"]
        assert [s.workflow for s in proposals[0].steps] == [
            "rebase-stack", "full-verify", "smoke-test"
        ]

    def test_proposals_do_not_become_workflows_on_their_own(self, session_manager, job, miner):
        session_manager._finish_turn(miner, f"```json\n{json.dumps(PROPOSAL)}\n```")
        assert "post-rebase-verify" not in workflow_names()

    def test_the_user_is_asked_to_decide(self, session_manager, job, miner):
        session_manager._finish_turn(miner, f"```json\n{json.dumps(PROPOSAL)}\n```")
        reasons = [item.reason for item in session_manager.list_attention_items()]
        assert any("post-rebase-verify" in reason for reason in reasons)

    def test_proposing_nothing_asks_for_nothing(self, session_manager, job, miner):
        session_manager._finish_turn(miner, '```json\n{"proposals": []}\n```')
        assert session_manager.list_proposals(job.id) == []
        assert session_manager.list_attention_items() == []


class TestAccepting:
    def test_accepting_writes_an_ordinary_user_workflow(
        self, session_manager, job, miner, isolated_workflows
    ):
        session_manager._finish_turn(miner, f"```json\n{json.dumps(PROPOSAL)}\n```")
        path = session_manager.accept_proposal(job.id, "post-rebase-verify")

        assert path.parent == isolated_workflows
        written = yaml.safe_load(path.read_text())
        assert written["name"] == "post-rebase-verify"
        assert [s["workflow"] for s in written["steps"]] == [
            "rebase-stack", "full-verify", "smoke-test"
        ]

    def test_an_accepted_proposal_is_immediately_routable(self, session_manager, job, miner):
        session_manager._finish_turn(miner, f"```json\n{json.dumps(PROPOSAL)}\n```")
        session_manager.accept_proposal(job.id, "post-rebase-verify")

        assert "post-rebase-verify" in workflow_names()
        assert get_workflow("post-rebase-verify").is_composite

    def test_accepting_is_recorded(self, session_manager, job, miner):
        session_manager._finish_turn(miner, f"```json\n{json.dumps(PROPOSAL)}\n```")
        session_manager.accept_proposal(job.id, "post-rebase-verify")
        assert ev.WORKFLOW_PROPOSAL_ACCEPTED in [e.kind for e in session_manager.store.recent_events()]

    def test_an_unknown_proposal_is_refused(self, session_manager, job, miner):
        session_manager._finish_turn(miner, f"```json\n{json.dumps(PROPOSAL)}\n```")
        with pytest.raises(SessionManagerError, match="No proposal named"):
            session_manager.accept_proposal(job.id, "something-else")

    def test_a_proposal_naming_a_workflow_that_does_not_exist_is_refused(
        self, session_manager, job, miner
    ):
        bad = {
            "proposals": [
                {
                    "name": "invented",
                    "description": "x",
                    "steps": [{"workflow": "does-not-exist"}],
                }
            ]
        }
        session_manager._finish_turn(miner, f"```json\n{json.dumps(bad)}\n```")
        with pytest.raises(SessionManagerError, match="do not exist"):
            session_manager.accept_proposal(job.id, "invented")

    def test_a_proposal_with_an_invented_condition_is_refused_before_anything_is_written(
        self, session_manager, job, miner, isolated_workflows
    ):
        """The condition is free text from a model, and the prompt lists the legal values
        as prose -- exactly the kind of thing a model paraphrases."""
        bad = {
            "proposals": [
                {
                    "name": "paraphrased",
                    "description": "x",
                    "steps": [{"workflow": "full-verify", "when": "whenever tests fail"}],
                }
            ]
        }
        session_manager._finish_turn(miner, f"```json\n{json.dumps(bad)}\n```")
        with pytest.raises(SessionManagerError, match="not a valid workflow"):
            session_manager.accept_proposal(job.id, "paraphrased")
        assert not list(isolated_workflows.glob("*.yaml"))

    def test_a_proposal_may_not_take_over_a_builtin_name(self, session_manager, job, miner):
        bad = {
            "proposals": [
                {
                    "name": "implement-approved-plan",
                    "description": "No contract needed.",
                    "steps": [{"workflow": "full-verify"}],
                }
            ]
        }
        session_manager._finish_turn(miner, f"```json\n{json.dumps(bad)}\n```")
        with pytest.raises(SessionManagerError, match="built-in"):
            session_manager.accept_proposal(job.id, "implement-approved-plan")
        assert get_workflow("implement-approved-plan").requires  # untouched

    def test_a_proposal_may_not_nest_a_composite(self, session_manager, job, miner):
        bad = {
            "proposals": [
                {"name": "nested", "description": "x", "steps": [{"workflow": "complete-ticket"}]}
            ]
        }
        session_manager._finish_turn(miner, f"```json\n{json.dumps(bad)}\n```")
        with pytest.raises(SessionManagerError, match="cannot nest"):
            session_manager.accept_proposal(job.id, "nested")

    def test_an_accepted_proposal_actually_loads(self, session_manager, job, miner):
        """The user is told the workflow exists, so it must exist."""
        session_manager._finish_turn(miner, f"```json\n{json.dumps(PROPOSAL)}\n```")
        path = session_manager.accept_proposal(job.id, "post-rebase-verify")
        assert path.exists()
        assert "post-rebase-verify" in workflow_names()
        assert not any("post-rebase-verify" in p for p in session_manager.reload_workflows())

    def test_a_proposal_with_no_steps_is_refused(self, session_manager, job, miner):
        empty = {"proposals": [{"name": "hollow", "description": "x", "steps": []}]}
        session_manager._finish_turn(miner, f"```json\n{json.dumps(empty)}\n```")
        with pytest.raises(SessionManagerError, match="no steps"):
            session_manager.accept_proposal(job.id, "hollow")

    def test_an_existing_file_is_never_overwritten(
        self, session_manager, job, miner, isolated_workflows
    ):
        session_manager._finish_turn(miner, f"```json\n{json.dumps(PROPOSAL)}\n```")
        session_manager.accept_proposal(job.id, "post-rebase-verify")
        with pytest.raises(SessionManagerError, match="already exists"):
            session_manager.accept_proposal(job.id, "post-rebase-verify")
