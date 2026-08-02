"""The session-first board, driven headlessly through Textual's pilot."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Button, Input, Static

from switchboard.agents.manager import DeterministicManager
from switchboard.app import Services
from switchboard.config import Config
from switchboard.domain.enums import WorkerStatus
from switchboard.ui.screens import SwitchboardApp
from tests.conftest import TICKET

SECOND_TICKET = """ENG-999 Rewrite the billing exporter

The nightly billing exporter times out on large tenants and needs a streaming
rewrite so memory stays flat. Acceptance: exports finish under ten minutes.
"""


@pytest.fixture
def app(session_manager, backend, git_repo, worktree_service, store) -> SwitchboardApp:
    repo_path = git_repo("alpha")
    session_manager.register_repository(repo_path, "alpha")
    services = Services(
        config=Config(),
        store=store,
        backend=backend,
        worktrees=worktree_service,
        session_manager=session_manager,
        manager=DeterministicManager(session_manager),
        scripted=True,
    )
    application = SwitchboardApp(
        services.session_manager, services.manager, startup_notes=["scripted backend"]
    )
    application.services = services  # type: ignore[attr-defined]
    return application


async def quiet(pilot, ticks: int = 12) -> None:
    """Let manager turns, backend pumps, and the UI refresh settle."""
    for _ in range(ticks):
        await pilot.pause()
        await asyncio.sleep(0.02)


def rendered_text(pilot, selector: str) -> str:
    """The text a Static is currently displaying."""
    return str(pilot.app.query_one(selector, Static).content)


async def send_to_manager(pilot, text: str) -> None:
    manager_input = pilot.app.query_one("#manager-input", Input)
    manager_input.value = text
    await pilot.app._manager_turn(text)
    manager_input.value = ""
    await quiet(pilot)


async def test_the_window_is_session_first_with_one_manager_input(app):
    async with app.run_test() as pilot:
        await quiet(pilot)
        assert pilot.app.query_one("#manager-pane")
        assert pilot.app.query_one("#worker-list-pane")
        assert pilot.app.query_one("#worker-pane")
        # Claude owns direct session conversation; the board has one orchestration input.
        assert len(pilot.app.query(Input)) == 1
        assert pilot.app.query_one("#manager-input", Input)
        assert "Manager" in pilot.app.worker_list_pane._signature[0][1]
        assert pilot.app.query_one("#interrupt-button", Button)


async def test_pasting_a_ticket_creates_a_job_and_shows_its_worker(app):
    async with app.run_test() as pilot:
        await quiet(pilot)
        await send_to_manager(pilot, TICKET)

        jobs = app.sm.store.list_jobs()
        assert len(jobs) == 1 and jobs[0].external_ref == "ENG-421"
        assert app.worker_list_pane.worker_ids, "the planner appears in the worker list"

        planner = app.sm.store.list_workers()[0]
        assert planner.status is WorkerStatus.BLOCKED


async def test_the_blocked_worker_renders_an_application_owned_banner(app):
    async with app.run_test() as pilot:
        await quiet(pilot)
        await send_to_manager(pilot, TICKET)
        planner = app.sm.store.list_workers()[0]
        app.select_worker(planner.id)
        await quiet(pilot)

        header = rendered_text(pilot, "#worker-header")
        banner = rendered_text(pilot, "#attention-banner")
        assert "ENG-421" in header and "planner" in header and "blocked" in header
        assert "Reason:" in banner and "Waiting for:" in banner


async def test_selecting_a_worker_shows_durable_orchestration_state(app):
    async with app.run_test() as pilot:
        await quiet(pilot)
        await send_to_manager(pilot, TICKET)
        planner = app.sm.store.list_workers()[0]
        app.select_worker(planner.id)
        await quiet(pilot)

        rendered = rendered_text(pilot, "#session-detail")
        assert "workflow" in rendered
        assert "plan-feature" in rendered
        assert "lifecycle" in rendered
        assert "evidence" in rendered


async def test_worker_detail_has_no_custom_reply_or_transcript_surface(app):
    async with app.run_test() as pilot:
        await quiet(pilot)
        await send_to_manager(pilot, TICKET)
        planner = app.sm.store.list_workers()[0]
        app.select_worker(planner.id)
        await quiet(pilot)

        assert len(pilot.app.query(Input)) == 1
        assert not pilot.app.query("#worker-input")
        assert not pilot.app.query("#transcript")
        assert "Enter" in rendered_text(pilot, "#enter-hint")


async def test_the_ui_never_auto_switches_while_the_user_is_typing(app):
    async with app.run_test() as pilot:
        await quiet(pilot)
        await send_to_manager(pilot, TICKET)
        await send_to_manager(pilot, SECOND_TICKET)
        assert len(app.sm.store.list_jobs()) == 2, "an unrelated ticket gets its own job"

        first = app.sm.store.list_workers()[0]
        app.select_worker(first.id)
        await quiet(pilot)
        before = app.sm.selected_worker_id

        typing = pilot.app.query_one("#manager-input", Input)
        typing.focus()
        typing.value = "half a thought"
        await quiet(pilot, ticks=2)
        assert app.user_is_typing is True
        app.maybe_auto_advance()
        await quiet(pilot)
        assert app.sm.selected_worker_id == before, "focus must not move mid-message"

        typing.value = ""
        assert app.user_is_typing is False


async def test_auto_advance_can_be_paused_and_workers_pinned(app):
    async with app.run_test() as pilot:
        await quiet(pilot)
        await send_to_manager(pilot, TICKET)
        worker = app.sm.store.list_workers()[0]
        app.select_worker(worker.id)
        await quiet(pilot)

        assert app.sm.auto_advance is True
        await pilot.press("ctrl+a")
        await quiet(pilot, ticks=4)
        assert app.sm.auto_advance is False
        await pilot.press("ctrl+a")
        await quiet(pilot, ticks=4)
        assert app.sm.auto_advance is True

        await pilot.press("ctrl+p")
        await quiet(pilot, ticks=4)
        assert app.sm.store.get_worker(worker.id).pinned is True


async def test_worker_entry_returns_ownership_after_empty_composer_confirmation(
    app, monkeypatch
):
    async with app.run_test() as pilot:
        await quiet(pilot)
        await send_to_manager(pilot, TICKET)
        worker = app.sm.store.list_workers()[0]
        app.select_worker(worker.id)
        monkeypatch.setattr(
            "switchboard.ui.screens.subprocess.run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0),
        )
        monkeypatch.setattr("builtins.input", lambda prompt: "yes")
        monkeypatch.setattr(app, "suspend", lambda: nullcontext())

        await app.action_attach()
        await app.action_attach()
        await quiet(pilot)

        assert not app.sm.is_attached(worker.id)
        returns = sum(
            "Back in Switchboard" in entry for entry in app.manager_pane.entries
        )
        assert returns >= 2


async def test_the_manager_pane_window_stays_bounded(app):
    async with app.run_test() as pilot:
        await quiet(pilot)
        for index in range(12):
            await send_to_manager(pilot, f"Is setting {index} shared between requests?")
        assert len(app.manager_pane.entries) <= 8


async def test_a_normal_goal_does_not_hide_the_manager_outcome(app):
    async with app.run_test(size=(80, 24)) as pilot:
        await quiet(pilot)
        app.sm.store.set_preference("manager.current_objective", "x" * 120)
        app.manager_pane.add_note("An older recovery note that must not occupy the viewport.")
        app.manager_pane.complete_exchange("Visible manager outcome.")
        app._tick()
        await quiet(pilot)

        assert "Visible manager outcome." in rendered_text(pilot, "#manager-log")


async def test_manager_title_does_not_claim_ready_while_board_turn_is_busy(app, monkeypatch):
    monkeypatch.setattr(
        app.manager,
        "status",
        lambda: {"state": "ready", "owner": "manager"},
        raising=False,
    )
    async with app.run_test(size=(80, 24)) as pilot:
        await quiet(pilot)
        app._busy = True
        app._tick()
        await quiet(pilot)

        assert "Manager · turn active · manager" in rendered_text(pilot, "#manager-title")


async def test_manager_startup_instruction_uses_global_entry_binding(app, monkeypatch):
    monkeypatch.setattr(
        app.manager,
        "status",
        lambda: {"state": "starting", "owner": "manager"},
        raising=False,
    )
    async with app.run_test(size=(80, 24)) as pilot:
        await quiet(pilot)
        detail = rendered_text(pilot, "#session-detail")
        assert "Press Ctrl+E" in detail
        assert "Press Enter to handle" not in detail


async def test_entering_the_scripted_manager_explains_why_it_cannot_be_entered(app):
    """Every manager answers `enter`; the offline one refuses with a reason.

    The UI must not silently do nothing when a session cannot be entered -- that was
    indistinguishable from a key that did not register.
    """
    async with app.run_test() as pilot:
        await quiet(pilot)
        app.action_focus_manager()
        await app.action_attach()
        await quiet(pilot)

        assert any(
            "Cannot enter that session" in entry and "rule engine" in entry
            for entry in app.manager_pane.entries
        )


async def test_the_help_screen_lists_the_bindings(app):
    async with app.run_test() as pilot:
        await quiet(pilot)
        app.query_one("#worker-table").focus()
        await pilot.press("question_mark")
        await quiet(pilot, ticks=4)
        assert type(pilot.app.screen).__name__ == "HelpScreen"
        await pilot.press("escape")
        await quiet(pilot, ticks=4)
        assert type(pilot.app.screen).__name__ == "MainScreen"


def test_the_documented_terminal_captures_exist():
    docs = Path(__file__).resolve().parents[2] / "docs"
    captures = sorted(docs.glob("ui-*.txt"))
    assert len(captures) >= 3
    for capture in captures:
        text = capture.read_text()
        assert "Manager" in text and "Jobs" in text


# ------------------------------------------------------- jobs, not a flat fleet


async def test_the_board_is_organised_around_jobs_with_their_sessions_under_them(app):
    """The user thinks in pieces of work, not in a list of processes."""
    async with app.run_test() as pilot:
        await quiet(pilot)
        await send_to_manager(pilot, TICKET)
        await send_to_manager(pilot, SECOND_TICKET)
        await quiet(pilot)

        keys = [key for key, _ in app.worker_list_pane._signature]
        assert keys[0] == "manager"
        job_rows = [key for key in keys if key.startswith("job:")]
        assert len(job_rows) == 2, "one row per job"
        # Each job row is immediately followed by its own session.
        for index, key in enumerate(keys):
            if key.startswith("job:"):
                assert not keys[index + 1].startswith("job:"), "its sessions come next"

        title = rendered_text(pilot, "#worker-list-title")
        assert "Jobs (2)" in title and "sessions (2)" in title


async def test_a_job_row_shows_its_workflow_and_where_the_run_stands(app):
    async with app.run_test() as pilot:
        await quiet(pilot)
        await send_to_manager(pilot, TICKET)
        await quiet(pilot)

        rows = dict(app.worker_list_pane._signature)
        job_row = next(text for key, text in rows.items() if key.startswith("job:"))
        assert "ENG-421" in job_row
        assert "complete-ticket" in job_row
        assert "step 1/" in job_row, "progress through the workflow, not just a stage name"


async def test_stepping_through_sessions_never_lands_on_a_job_heading(app):
    async with app.run_test() as pilot:
        await quiet(pilot)
        await send_to_manager(pilot, TICKET)
        await send_to_manager(pilot, SECOND_TICKET)
        await quiet(pilot)

        ids = app.worker_list_pane.worker_ids
        assert len(ids) == 2
        real = {worker.id for worker in app.sm.store.list_workers()}
        assert set(ids) <= real


async def test_selecting_a_job_answers_why_it_is_not_done_yet(app):
    async with app.run_test() as pilot:
        await quiet(pilot)
        await send_to_manager(pilot, TICKET)
        await quiet(pilot)

        app.sm.auto_advance = False  # otherwise the queue reselects the blocked planner
        job = app.sm.store.list_jobs()[0]
        app._selected_manager = False
        app._selected_job_id = job.id
        app.refresh_worker_pane()
        await quiet(pilot)

        header = rendered_text(pilot, "#worker-header")
        detail = rendered_text(pilot, "#session-detail")
        assert "ENG-421" in header
        assert "complete-ticket" in detail
        assert "complete" in detail and "no" in detail
        assert "needs" in detail, "it names the definition of done"
        assert any(
            blocker in detail for blocker in ("verification", "review", "implementation")
        ), "and the specific blockers"


async def test_picking_a_job_opens_the_session_that_needs_the_user(app):
    async with app.run_test() as pilot:
        await quiet(pilot)
        await send_to_manager(pilot, TICKET)
        await quiet(pilot)

        job = app.sm.store.list_jobs()[0]
        planner = app.sm.store.list_workers(job.id)[0]
        assert app._worker_for_job(job.id) == planner.id
