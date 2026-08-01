"""Entering a worker's session directly.

The product claim is that a worker is an ordinary Claude session: the user can step
into it exactly as if they had opened its worktree and run `claude`. These tests hold
Switchboard to that -- the command it produces, and what it does to its own state first.
"""

from __future__ import annotations

import asyncio

import pytest

from switchboard.agents.attach import AttachError, build_attachment
from switchboard.agents.scripted_backend import ScriptedWorkerBackend
from switchboard.config import Config
from switchboard.core.session_manager import SessionManager, SessionManagerError
from switchboard.domain import events as ev
from switchboard.domain.enums import ArtifactType, RunStatus, RuntimeOwner, WorkerRole, WorkerStatus
from switchboard.domain.models import Artifact
from switchboard.gitops import runner
from switchboard.routing import router
from tests.conftest import commit_file


class TestAttachmentCommand:
    def test_it_is_the_command_the_user_would_have_typed(self, tmp_path):
        attachment = build_attachment(cwd=tmp_path, session_id="sess-1")
        assert attachment.argv == ["claude", "--resume", "sess-1"]
        assert attachment.shell_hint == f"cd {tmp_path} && claude --resume sess-1"

    def test_it_honours_the_configured_executable(self, tmp_path):
        attachment = build_attachment(
            cwd=tmp_path, session_id="sess-1", executable="/opt/company-claude"
        )
        assert attachment.argv[0] == "/opt/company-claude"

    def test_a_worker_with_no_session_cannot_be_entered(self, tmp_path):
        with pytest.raises(AttachError, match="no Claude session"):
            build_attachment(cwd=tmp_path, session_id=None)

    def test_a_vanished_working_directory_is_reported(self, tmp_path):
        with pytest.raises(AttachError, match="no longer exists"):
            build_attachment(cwd=tmp_path / "gone", session_id="sess-1")


@pytest.fixture
async def worker(session_manager, git_repo):
    repo = session_manager.register_repository(git_repo("attachable"))
    job = session_manager.create_job("ENG-9 Payments", repo.id, external_ref="ENG-9")
    worker = await session_manager.create_worker(
        role=WorkerRole.IMPLEMENTER,
        title="Implement ENG-9",
        prompt="",
        job_id=job.id,
        repository_id=repo.id,
        writable=True,
    )
    worker.session_id = "sess-abc"
    session_manager.store.save_worker(worker)
    return worker


class TestSessionManagerAttach:
    async def test_it_returns_a_command_rooted_in_the_workers_own_worktree(
        self, session_manager, worker
    ):
        attachment = await session_manager.attach(worker.id)
        stored = session_manager.store.get_worker(worker.id)
        assert attachment.cwd == worker.cwd
        assert attachment.session_id == stored.session_id
        assert attachment.argv[1:] == ["--resume", stored.session_id]

    async def test_a_working_worker_is_interrupted_first(self, session_manager, worker):
        session_manager._set_status(worker, WorkerStatus.WORKING)
        await session_manager.attach(worker.id)
        assert session_manager.store.get_worker(worker.id).status is WorkerStatus.IDLE

    async def test_the_handover_is_recorded_as_an_event(self, session_manager, worker):
        await session_manager.attach(worker.id)
        kinds = [e.kind for e in session_manager.store.recent_events()]
        assert ev.WORKER_ATTACHED in kinds

    async def test_the_transcript_says_the_user_took_over(self, session_manager, worker):
        await session_manager.attach(worker.id)
        transcript = session_manager.store.transcript(worker.id)
        assert any("attached" in message.text for message in transcript)

    async def test_a_running_composite_run_pauses(self, session_manager, worker):
        run = await session_manager.start_run("complete-ticket", job_id=worker.job_id)
        current = session_manager.store.get_run(run.id)
        assert current.status is RunStatus.RUNNING
        assert current.current_worker_id is not None

        # The run's worker is brand new, so give it the session id its backend would.
        running = session_manager.store.get_worker(current.current_worker_id)
        running.session_id = "sess-run"
        session_manager.store.save_worker(running)

        await session_manager.attach(running.id)

        # What happens next is the user's decision now, not the run's.
        assert session_manager.store.get_run(run.id).status is RunStatus.BLOCKED

    async def test_a_worker_without_a_session_is_refused(self, session_manager, worker):
        worker.session_id = None
        session_manager.store.save_worker(worker)
        with pytest.raises(AttachError):
            await session_manager.attach(worker.id)


class TestAttachHandsOverControl:
    async def test_switchboard_refuses_to_send_while_the_user_is_attached(self, session_manager, worker):
        """Two writers would interleave turns in one session file."""
        await session_manager.attach(worker.id)
        with pytest.raises(SessionManagerError, match="attached"):
            await session_manager.send(worker.id, "keep going")

    async def test_attachment_ownership_survives_a_session_manager_restart(
        self, session_manager, worker, backend
    ):
        await session_manager.attach(worker.id)
        restarted = SessionManager(
            session_manager.store, backend, Config(), session_manager.worktrees
        )

        assert restarted.is_attached(worker.id)
        assert restarted.store.current_runtime(worker.id).owner is RuntimeOwner.HUMAN
        with pytest.raises(SessionManagerError, match="attached"):
            await restarted.send(worker.id, "do not interleave")

    async def test_recovery_does_not_recreate_an_unobservable_human_owned_runtime(
        self, session_manager, worker
    ):
        await session_manager.attach(worker.id)
        restarted = SessionManager(
            session_manager.store,
            ScriptedWorkerBackend(),
            Config(),
            session_manager.worktrees,
        )

        notes = await restarted.recover()

        assert any("human-owned runtime not observable" in note for note in notes)
        assert restarted.store.get_worker(worker.id).status is WorkerStatus.DISCONNECTED
        assert len(restarted.store.list_runtimes(worker.id)) == 1

    async def test_leaving_gives_control_back(self, session_manager, worker):
        await session_manager.attach(worker.id)
        session_manager.detach(worker.id)
        assert not session_manager.is_attached(worker.id)
        assert session_manager.store.current_runtime(worker.id).owner is RuntimeOwner.MANAGER
        await session_manager.send(worker.id, "keep going")  # must not raise

    async def test_the_paused_run_can_be_resumed_after_leaving(self, session_manager, worker):
        """Attaching must not be a one-way door for the ritual."""
        run = await session_manager.start_run("complete-ticket", job_id=worker.job_id)
        current = session_manager.store.get_run(run.id)
        running = session_manager.store.get_worker(current.current_worker_id)
        running.session_id = "sess-run"
        session_manager.store.save_worker(running)

        await session_manager.attach(running.id)
        session_manager.detach(running.id)
        assert session_manager.store.get_run(run.id).status is RunStatus.BLOCKED

        resumed = await session_manager.resume_run(run.id)
        assert resumed.status is not RunStatus.BLOCKED

    async def test_attaching_to_a_read_only_observer_warns_about_the_worktree(
        self, session_manager, worker
    ):
        """A read-only worker sits in the *writable* worker's tree, and `claude --resume`
        has none of the tool restrictions that made it read-only."""
        reviewer = await session_manager.create_worker(
            role=WorkerRole.REVIEWER,
            title="Review ENG-9",
            prompt="",
            job_id=worker.job_id,
            repository_id=worker.repository_id,
            writable=False,
        )
        reviewer.session_id = "sess-rev"
        session_manager.store.save_worker(reviewer)

        attachment = await session_manager.attach(reviewer.id)
        assert attachment.cwd == worker.cwd  # it observes the implementer's worktree
        assert "read-only" in attachment.note
        assert worker.title in attachment.note

    async def test_attaching_to_a_writable_worker_needs_no_warning(self, session_manager, worker):
        assert (await session_manager.attach(worker.id)).note == ""

    async def test_detach_invalidates_evidence_changed_during_human_control(
        self, session_manager, worker
    ):
        head = runner.head_commit(worker.cwd)
        session_manager.store.save_artifact(
            Artifact(
                job_id=worker.job_id,
                worker_id=worker.id,
                type=ArtifactType.VERIFICATION,
                head_commit=head,
                tree_hash=runner.tree_hash(worker.cwd),
                body={"evidence": []},
            )
        )

        await session_manager.attach(worker.id)
        for _ in range(5):
            await asyncio.sleep(0)
        commit_file(worker.cwd, "human.txt", "changed\n", "human edit")
        session_manager.detach(worker.id)

        artifact = session_manager.store.latest_artifact(
            worker.job_id, ArtifactType.VERIFICATION
        )
        assert artifact.stale
        assert "implementation_edit" in artifact.stale_reason


class TestAttachRouting:
    async def test_asking_to_be_let_in_routes_to_the_selected_worker(
        self, session_manager, worker
    ):
        session_manager.selected_worker_id = worker.id
        state = session_manager.routing_state()
        proposal = router.resolve_route("let me into that session", state)
        assert proposal.action == "attach_worker"
        assert proposal.worker_id == worker.id

    async def test_a_ticket_reference_resolves_the_worker(self, session_manager, worker):
        session_manager.selected_worker_id = None
        proposal = router.resolve_route("drop me into ENG-9", session_manager.routing_state())
        assert proposal.action == "attach_worker"
        assert proposal.worker_id == worker.id

    async def test_attaching_beats_a_workflow_phrase_in_the_same_sentence(
        self, session_manager, worker
    ):
        session_manager.selected_worker_id = worker.id
        state = session_manager.routing_state()
        proposal = router.resolve_route("let me in, I'll rebase it myself", state)
        assert proposal.action == "attach_worker"

    async def test_a_ticket_that_matches_no_job_asks_rather_than_guessing(
        self, session_manager, worker
    ):
        """Attaching interrupts a worker and pauses its run, so a wrong guess is not free."""
        session_manager.selected_worker_id = worker.id
        state = session_manager.routing_state()
        proposal = router.resolve_route("drop me into ENG-777", state)
        assert proposal.action == "clarify"

    async def test_asking_the_manager_to_take_over_a_task_is_not_an_attach(
        self, session_manager, worker
    ):
        session_manager.selected_worker_id = worker.id
        state = session_manager.routing_state()
        assert router.resolve_route("can you take over from here", state).action != "attach_worker"

    async def test_resuming_a_run_is_routable(self, session_manager, worker):
        session_manager.selected_worker_id = worker.id
        state = session_manager.routing_state()
        proposal = router.resolve_route("resume the run", state)
        assert proposal.action == "resume_run"
        assert proposal.job_id == worker.job_id

    async def test_with_nothing_to_attach_to_it_asks(self, session_manager):
        state = session_manager.routing_state()
        proposal = router.resolve_route("let me into that session", state)
        assert proposal.action == "clarify"

    async def test_executing_the_route_reports_the_command(self, session_manager, worker):
        session_manager.selected_worker_id = worker.id
        state = session_manager.routing_state()
        proposal = router.resolve_route("let me into that session", state)
        reply = await session_manager.execute_route(proposal)
        stored = session_manager.store.get_worker(worker.id)
        assert f"claude --resume {stored.session_id}" in reply
        assert str(worker.cwd) in reply
