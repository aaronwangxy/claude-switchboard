"""The three-pane UI, driven headlessly through Textual's pilot."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Button, Input, Static

from csm.agents.manager import DeterministicManager
from csm.app import Services
from csm.config import Config
from csm.domain.enums import WorkerStatus
from csm.ui.screens import CsmApp
from tests.conftest import TICKET

SECOND_TICKET = """ENG-999 Rewrite the billing exporter

The nightly billing exporter times out on large tenants and needs a streaming
rewrite so memory stays flat. Acceptance: exports finish under ten minutes.
"""


@pytest.fixture
def app(session_manager, backend, git_repo, worktree_service, store) -> CsmApp:
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
    application = CsmApp(
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


async def test_the_window_has_all_three_panes_and_one_manager_input(app):
    async with app.run_test() as pilot:
        await quiet(pilot)
        assert pilot.app.query_one("#manager-pane")
        assert pilot.app.query_one("#worker-list-pane")
        assert pilot.app.query_one("#worker-pane")
        # One manager input and one worker input: no ticket form, wizard, or mode switch.
        assert len(pilot.app.query(Input)) == 2
        assert pilot.app.query_one("#manager-input", Input)
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
        assert "ENG-421" in header and "Planner" in header and "Blocked" in header
        assert "Reason:" in banner and "Waiting for:" in banner


async def test_selecting_a_worker_restores_its_transcript(app):
    async with app.run_test() as pilot:
        await quiet(pilot)
        await send_to_manager(pilot, TICKET)
        planner = app.sm.store.list_workers()[0]
        app.select_worker(planner.id)
        await quiet(pilot)

        rendered = rendered_text(pilot, "#transcript")
        stored = app.sm.store.transcript(planner.id)
        assert stored
        assert stored[-1].text.splitlines()[0][:40] in rendered


async def test_a_follow_up_through_the_worker_input_is_recorded(app):
    async with app.run_test() as pilot:
        await quiet(pilot)
        await send_to_manager(pilot, TICKET)
        planner = app.sm.store.list_workers()[0]
        app.select_worker(planner.id)
        await quiet(pilot)

        await pilot.app._send_to_worker(planner.id, "Use the read-legacy strategy.")
        await quiet(pilot)
        texts = [m.text for m in app.sm.store.transcript(planner.id)]
        assert "Use the read-legacy strategy." in texts


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

        typing = pilot.app.query_one("#worker-input", Input)
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


async def test_the_manager_pane_window_stays_bounded(app):
    async with app.run_test() as pilot:
        await quiet(pilot)
        for index in range(12):
            await send_to_manager(pilot, f"Is setting {index} shared between requests?")
        assert len(app.manager_pane.entries) <= 8


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
        assert "Manager" in text and "Workers" in text
