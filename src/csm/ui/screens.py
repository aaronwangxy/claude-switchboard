"""The three-pane Textual UI.

The widgets here own presentation only. Every behaviour -- routing a message, starting a
worker, interrupting one, pinning, snoozing -- is a call into `SessionManager` or the
manager agent, and everything rendered comes back out of the store. There is no Git, no
SQLite and no worktree logic in this module.

Layout::

    +------------------------------+----------------------------------------------+
    | Manager (top-left)           | Selected worker (right)                      |
    |  bounded recent conversation |  attention banner                            |
    |  one universal input         |  full transcript, streaming                  |
    +------------------------------+  follow-up input + Interrupt button          |
    | Workers / attention (bottom) |                                              |
    +------------------------------+----------------------------------------------+
"""

from __future__ import annotations

import logging
import subprocess
from collections import deque
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING
from uuid import UUID

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from csm.core.session_manager import SessionManager
from csm.domain.enums import WorkerStatus
from csm.domain.models import AttentionItem, Event, Worker
from csm.routing.attention import next_actionable, prioritize
from csm.ui.help import HelpScreen

if TYPE_CHECKING:  # imported for typing only, so the UI stays free of agent internals
    from csm.agents.manager import Manager

log = logging.getLogger(__name__)

#: The manager pane is a bounded window on the conversation, not a transcript.
MAX_MANAGER_EXCHANGES = 8

#: How much of a worker's `waiting_for` fits on one list row.
REASON_WIDTH = 60

PENDING = "…"


# --------------------------------------------------------------------------- panes


class ManagerPane(Vertical):
    """A bounded log of recent exchanges plus the one universal input.

    There is deliberately no ticket form, intake panel or workflow picker: pasting a
    ticket is an ordinary message.
    """

    def __init__(self) -> None:
        super().__init__(id="manager-pane")
        self._entries: deque[list[str | None]] = deque(maxlen=MAX_MANAGER_EXCHANGES)

    def compose(self) -> ComposeResult:
        yield Static("Manager", classes="pane-title")
        with VerticalScroll(id="manager-scroll"):
            yield Static(id="manager-log")
        yield Input(
            placeholder="Message the manager — paste a ticket, ask a question, give an instruction",
            id="manager-input",
        )

    def on_mount(self) -> None:
        self._repaint()

    # ------------------------------------------------------------------ content

    def add_note(self, text: str) -> None:
        """Record an application note (startup, recovery, refusals)."""
        self._entries.append([None, text])
        self._repaint()

    def begin_exchange(self, user_text: str) -> None:
        self._entries.append([user_text, PENDING])
        self._repaint()

    def complete_exchange(self, reply: str) -> None:
        if self._entries and self._entries[-1][1] == PENDING:
            self._entries[-1][1] = reply
        else:
            self._entries.append([None, reply])
        self._repaint()

    @property
    def entries(self) -> list[tuple[str | None, str | None]]:
        return [(entry[0], entry[1]) for entry in self._entries]

    def _repaint(self) -> None:
        text = Text()
        if not self._entries:
            text.append(
                "Nothing yet. Paste a ticket or ask a question below.\n", style="dim italic"
            )
        for user_text, reply in self._entries:
            if user_text is not None:
                text.append("you  ", style="bold cyan")
                text.append(f"{_compact(user_text)}\n")
            text.append("mgr  ", style="bold green")
            text.append(f"{reply or PENDING}\n\n")
        try:
            self.query_one("#manager-log", Static).update(text)
            self.query_one("#manager-scroll", VerticalScroll).scroll_end(animate=False)
        except Exception:  # not mounted yet
            pass


class WorkerListPane(Vertical):
    """One row per worker: attention first, then everything else."""

    def __init__(self) -> None:
        super().__init__(id="worker-list-pane")
        self._signature: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield Static("Workers", classes="pane-title", id="worker-list-title")
        table: DataTable = DataTable(id="worker-table", cursor_type="row", zebra_stripes=False)
        table.show_header = False
        yield table

    def on_mount(self) -> None:
        table = self.query_one("#worker-table", DataTable)
        table.add_column("worker", key="worker")

    def update_rows(self, rows: Sequence[tuple[Worker, Text]], selected: UUID | None) -> None:
        """Rebuild only when the rendered rows actually changed."""
        table = self.query_one("#worker-table", DataTable)
        signature = [(str(worker.id), row.plain) for worker, row in rows]
        if signature != self._signature:
            self._signature = signature
            table.clear()
            for worker, row in rows:
                table.add_row(row, key=str(worker.id))
        if selected is not None:
            index = next((i for i, (wid, _) in enumerate(signature) if wid == str(selected)), None)
            if index is not None and table.cursor_row != index:
                table.move_cursor(row=index)

    @property
    def worker_ids(self) -> list[UUID]:
        return [UUID(wid) for wid, _ in self._signature]


class WorkerPane(Vertical):
    """Header, application-owned attention banner, transcript, follow-up input."""

    def __init__(self) -> None:
        super().__init__(id="worker-pane")

    def compose(self) -> ComposeResult:
        yield Static("No worker selected", id="worker-header", classes="pane-title")
        yield Static("", id="attention-banner")
        with VerticalScroll(id="transcript-scroll"):
            yield Static(id="transcript")
        with Horizontal(id="worker-controls"):
            yield Input(placeholder="Reply to this worker", id="worker-input")
            yield Button("Interrupt", id="interrupt-button", variant="warning")

    def show_empty(self, message: str = "No worker selected") -> None:
        self.query_one("#worker-header", Static).update(message)
        banner = self.query_one("#attention-banner", Static)
        banner.update("")
        banner.display = False
        self.query_one("#transcript", Static).update(
            Text("Select a worker on the left, or start one from the manager.", style="dim italic")
        )
        self.query_one("#worker-input", Input).disabled = True
        self.query_one("#interrupt-button", Button).disabled = True

    def show_worker(
        self,
        header: str,
        item: AttentionItem | None,
        fallback_waiting_for: str | None,
        transcript: Text,
        scroll_to_end: bool = True,
    ) -> None:
        self.query_one("#worker-header", Static).update(header)
        banner = self.query_one("#attention-banner", Static)
        if item is None:
            banner.update("")
            banner.display = False
        else:
            waiting = item.waiting_for or fallback_waiting_for or "the user"
            text = Text()
            text.append("Reason: ", style="bold")
            text.append(f"{item.reason}\n")
            text.append("Waiting for: ", style="bold")
            text.append(waiting)
            banner.update(text)
            banner.display = True
        self.query_one("#transcript", Static).update(transcript)
        if scroll_to_end:
            self.query_one("#transcript-scroll", VerticalScroll).scroll_end(animate=False)
        self.query_one("#worker-input", Input).disabled = False
        self.query_one("#interrupt-button", Button).disabled = False


# -------------------------------------------------------------------------- screen


class MainScreen(Screen):
    """The three panes: manager and worker list on the left, selected worker on the right."""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            with Vertical(id="left-column"):
                yield ManagerPane()
                yield WorkerListPane()
            yield WorkerPane()
        yield Footer()


# ----------------------------------------------------------------------------- app


class CsmApp(App[None]):
    """The application shell. All behaviour is delegated to the session manager."""

    CSS_PATH = "app.tcss"
    TITLE = "Claude Session Manager"

    #: Textual binds Ctrl+P to its command palette; the documented pin binding wins.
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("ctrl+n", "focus_manager", "Manager", priority=True),
        Binding("ctrl+j", "next_worker", "Next worker", priority=True),
        Binding("ctrl+k", "previous_worker", "Prev worker", priority=True),
        Binding("ctrl+space,ctrl+at", "next_attention", "Next attention", priority=True),
        Binding("ctrl+p", "toggle_pin", "Pin", priority=True),
        Binding("ctrl+s", "snooze", "Snooze", priority=True),
        Binding("ctrl+a", "toggle_auto_advance", "Auto-advance", priority=True),
        Binding("ctrl+o", "interrupt", "Interrupt", priority=True),
        Binding("ctrl+e", "attach", "Enter session", priority=True),
        Binding("escape", "focus_workers", "Worker list"),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(
        self,
        session_manager: SessionManager,
        manager: Manager,
        startup_notes: Iterable[str] = (),
    ) -> None:
        super().__init__()
        self.sm = session_manager
        self.manager = manager
        self._startup_notes = list(startup_notes)
        self._busy = False
        #: Cheap fingerprint of what the worker pane last drew.
        self._pane_signature: tuple | None = None

    # ------------------------------------------------------------------- compose

    def get_default_screen(self) -> MainScreen:
        return MainScreen()

    # --------------------------------------------------------------------- panes

    @property
    def manager_pane(self) -> ManagerPane:
        return self.query_one(ManagerPane)

    @property
    def worker_list_pane(self) -> WorkerListPane:
        return self.query_one(WorkerListPane)

    @property
    def worker_pane(self) -> WorkerPane:
        return self.query_one(WorkerPane)

    # ------------------------------------------------------------------- startup

    def on_ready(self) -> None:
        """Runs once the default screen is composed (App.on_mount is too early)."""
        self.sm.subscribe(self._on_domain_event)
        self.worker_pane.show_empty()
        for note in self._startup_notes:
            self.manager_pane.add_note(note)
        self.refresh_workers()
        self.set_interval(1.0, self._tick)
        self.query_one("#manager-input", Input).focus()
        # Resuming sessions can be slow with a real backend, so it must not block the pump.
        self.run_worker(self._recover(), name="recover", exclusive=False)

    async def _recover(self) -> None:
        try:
            notes = await self.sm.recover()
        except Exception as exc:  # recovery must never stop the UI from coming up
            log.exception("recovery failed")
            self.manager_pane.add_note(f"Recovery failed: {exc}")
            return
        if notes:
            self.manager_pane.add_note("Recovered sessions: " + "; ".join(notes))
        else:
            self.manager_pane.add_note("Nothing to recover. " + self.sm.status_summary())
        self.refresh_workers()

    # -------------------------------------------------------------- live updates

    def _on_domain_event(self, event: Event) -> None:
        """Domain listener. Already on the event loop; defer to the message pump."""
        try:
            self.call_later(self._handle_domain_event, event)
        except Exception:  # the app may not be running yet
            log.debug("dropped event %s before the app was running", event.kind)

    def _handle_domain_event(self, event: Event) -> None:
        self.refresh_workers()
        if event.worker_id is not None and event.worker_id == self.sm.selected_worker_id:
            self.refresh_worker_pane()
        self.maybe_auto_advance()

    def _tick(self) -> None:
        """Low-frequency backstop in case a listener call was missed."""
        self.refresh_workers()
        self.refresh_worker_pane()

    # ------------------------------------------------------------------ rendering

    def ordered_workers(self) -> list[tuple[Worker, AttentionItem | None]]:
        """Attention items first, in queue order, then every other worker."""
        workers = {worker.id: worker for worker in self.sm.store.list_workers()}
        ordered: list[tuple[Worker, AttentionItem | None]] = []
        seen: set[UUID] = set()
        for item in self.sm.list_attention_items():
            worker = workers.get(item.worker_id)
            if worker is not None and worker.id not in seen:
                seen.add(worker.id)
                ordered.append((worker, item))
        for worker in workers.values():
            if worker.id not in seen:
                ordered.append((worker, None))
        return ordered

    def refresh_workers(self) -> None:
        rows = self.ordered_workers()
        jobs = {job.id: job for job in self.sm.store.list_jobs()}
        repos = {repo.id: repo for repo in self.sm.store.list_repositories()}
        rendered = [
            (
                worker,
                _worker_row(
                    worker,
                    item,
                    jobs.get(worker.job_id) if worker.job_id else None,
                    repos.get(worker.repository_id),
                ),
            )
            for worker, item in rows
        ]
        pane = self.worker_list_pane
        pane.update_rows(rendered, self.sm.selected_worker_id)
        attention_count = sum(1 for _, item in rows if item is not None)
        title = f"Workers ({len(rows)})"
        if attention_count:
            title += f" · {attention_count} need you"
        title += "  ·  auto-advance " + ("on" if self.sm.auto_advance else "off")
        self.query_one("#worker-list-title", Static).update(title)

    def refresh_worker_pane(self) -> None:
        """Redraw only when something actually changed, so scrolling is not fought."""
        worker_id = self.sm.selected_worker_id
        worker = self.sm.store.get_worker(worker_id) if worker_id else None
        if worker is None:
            if self._pane_signature is not None:
                self._pane_signature = None
                self.worker_pane.show_empty()
            return
        job = self.sm.store.get_job(worker.job_id) if worker.job_id else None
        label = (job.external_ref if job and job.external_ref else None) or worker.title
        header = f"{label} · {worker.role.value.title()} · {worker.status.value.title()}"
        if worker.pinned:
            header += " · pinned"
        item = next(
            (i for i in self.sm.list_attention_items() if i.worker_id == worker.id),
            None,
        )
        messages = self.sm.store.transcript(worker.id)
        signature = (
            str(worker.id),
            header,
            str(item.id) if item is not None else None,
            item.reason if item is not None else None,
            len(messages),
            messages[-1].text[-80:] if messages else "",
        )
        if signature == self._pane_signature:
            return
        self._pane_signature = signature
        # The transcript follows the tail whenever it changes; between changes the user
        # is free to scroll back without the redraw yanking the view.
        self.worker_pane.show_worker(header, item, worker.waiting_for, _render_transcript(messages))

    # ---------------------------------------------------------------- selection

    def select_worker(self, worker_id: UUID | None) -> None:
        self.sm.selected_worker_id = worker_id
        self.refresh_workers()
        self.refresh_worker_pane()

    @on(DataTable.RowHighlighted, "#worker-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        worker_id = _row_key_uuid(event.row_key)
        if worker_id is not None and worker_id != self.sm.selected_worker_id:
            self.sm.selected_worker_id = worker_id
            self.refresh_worker_pane()

    @on(DataTable.RowSelected, "#worker-table")
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        worker_id = _row_key_uuid(event.row_key)
        if worker_id is not None:
            self.select_worker(worker_id)

    # ------------------------------------------------------------- auto-advance

    @property
    def user_is_typing(self) -> bool:
        """True when a focused input holds unsent text."""
        focused = self.focused
        return isinstance(focused, Input) and bool(focused.value)

    def maybe_auto_advance(self) -> None:
        workers = {worker.id: worker for worker in self.sm.store.list_workers()}
        items = self.sm.store.list_attention_items()
        target = next_actionable(
            items,
            workers,
            current_worker_id=self.sm.selected_worker_id,
            auto_advance=self.sm.auto_advance,
            user_is_typing=self.user_is_typing,
        )
        if target is not None:
            self.select_worker(target)
            return
        if not self.sm.auto_advance or self.user_is_typing:
            return
        if prioritize(items, workers):
            return
        # Nothing is actionable: the manager input is where the next instruction goes.
        if not isinstance(self.focused, Input):
            self.query_one("#manager-input", Input).focus()

    # ------------------------------------------------------------------- inputs

    @on(Input.Submitted, "#manager-input")
    def _manager_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        if self._busy:
            self.manager_pane.add_note("Still working on the previous message.")
            return
        event.input.value = ""
        self.manager_pane.begin_exchange(text)
        # A manager turn can take seconds; run it off the message pump so the UI stays live.
        self.run_worker(self._manager_turn(text), name="manager-turn", exclusive=False)

    async def _manager_turn(self, text: str) -> None:
        self._busy = True
        try:
            reply = await self.manager.handle(text)
        except Exception as exc:
            log.exception("manager turn failed")
            reply = f"The manager could not handle that: {exc}"
        finally:
            self._busy = False
        self.manager_pane.complete_exchange(reply)
        self.refresh_workers()
        self.refresh_worker_pane()
        self.maybe_auto_advance()

    @on(Input.Submitted, "#worker-input")
    def _worker_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        worker_id = self.sm.selected_worker_id
        if not text or worker_id is None:
            return
        event.input.value = ""
        self.run_worker(self._send_to_worker(worker_id, text), name="worker-send")

    async def _send_to_worker(self, worker_id: UUID, text: str) -> None:
        try:
            await self.sm.send(worker_id, text)
        except Exception as exc:
            self.manager_pane.add_note(f"Could not send to the selected worker: {exc}")
        self.refresh_workers()
        self.refresh_worker_pane()

    @on(Button.Pressed, "#interrupt-button")
    async def _interrupt_pressed(self) -> None:
        await self.action_interrupt()

    # ------------------------------------------------------------------ actions

    def action_focus_manager(self) -> None:
        self.query_one("#manager-input", Input).focus()

    def action_focus_workers(self) -> None:
        self.query_one("#worker-table", DataTable).focus()

    def action_next_worker(self) -> None:
        self._step_worker(1)

    def action_previous_worker(self) -> None:
        self._step_worker(-1)

    def _step_worker(self, delta: int) -> None:
        ids = self.worker_list_pane.worker_ids
        if not ids:
            return
        current = self.sm.selected_worker_id
        index = ids.index(current) if current in ids else (-1 if delta > 0 else 0)
        self.select_worker(ids[(index + delta) % len(ids)])

    def action_next_attention(self) -> None:
        items = self.sm.list_attention_items()
        if not items:
            self.manager_pane.add_note(self.sm.status_summary())
            self.action_focus_manager()
            return
        current = self.sm.selected_worker_id
        target = next((i.worker_id for i in items if i.worker_id != current), items[0].worker_id)
        self.select_worker(target)

    def action_toggle_pin(self) -> None:
        worker_id = self.sm.selected_worker_id
        if worker_id is None:
            return
        worker = self.sm.toggle_pin(worker_id)
        self.manager_pane.add_note(
            f"{worker.title} is {'pinned' if worker.pinned else 'unpinned'}."
        )
        self.refresh_workers()
        self.refresh_worker_pane()

    def action_snooze(self) -> None:
        worker_id = self.sm.selected_worker_id
        if worker_id is None:
            return
        worker = self.sm.snooze(worker_id)
        self.manager_pane.add_note(f"{worker.title} is snoozed for 30 minutes.")
        self.refresh_workers()
        self.maybe_auto_advance()

    def action_toggle_auto_advance(self) -> None:
        self.sm.auto_advance = not self.sm.auto_advance
        self.manager_pane.add_note(
            f"Auto-advance is {'on' if self.sm.auto_advance else 'off'}."
        )
        self.refresh_workers()
        self.maybe_auto_advance()

    async def action_interrupt(self) -> None:
        worker_id = self.sm.selected_worker_id
        if worker_id is None:
            return
        try:
            await self.sm.interrupt_worker(worker_id)
        except Exception as exc:
            self.manager_pane.add_note(f"Could not interrupt that worker: {exc}")
        self.refresh_workers()
        self.refresh_worker_pane()

    async def action_attach(self) -> None:
        """Suspend CSM and hand the terminal to the selected worker's own session.

        The worker is an ordinary Claude session, so this runs the same `claude --resume`
        the user could have run themselves. CSM comes back when they exit it.
        """
        worker_id = self.sm.selected_worker_id
        if worker_id is None:
            return
        try:
            attachment = await self.sm.attach(worker_id)
        except Exception as exc:
            self.manager_pane.add_note(f"Cannot attach to that worker: {exc}")
            return
        self.manager_pane.add_note(f"Attaching: {attachment.shell_hint}")
        with self.suspend():
            try:
                subprocess.run(attachment.argv, cwd=attachment.cwd, check=False)
            except OSError as exc:  # a missing or unusable executable
                print(f"Could not start Claude: {exc}")
        self.manager_pane.add_note("Back in CSM. The worker is idle until you send it something.")
        self.refresh_workers()
        self.refresh_worker_pane()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())


# -------------------------------------------------------------------- rendering


def _compact(text: str, limit: int = 160) -> str:
    """One line, so a pasted ticket does not push the manager log off screen."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _marker(worker: Worker, item: AttentionItem | None) -> str:
    """! needs you, ● working, ✓ done or idle."""
    if item is not None:
        return "!"
    if worker.status in (WorkerStatus.WORKING, WorkerStatus.STARTING):
        return "●"
    if worker.status in (WorkerStatus.IDLE, WorkerStatus.DONE):
        return "✓"
    return "·"


def _worker_row(worker: Worker, item: AttentionItem | None, job, repo) -> Text:
    """One scannable line. The urgent parts come first because the pane is narrow."""
    marker = _marker(worker, item)
    style = (
        "bold red"
        if item is not None
        else ("bold" if worker.status is WorkerStatus.WORKING else "")
    )
    ref = job.external_ref if job is not None and job.external_ref else None
    title = worker.title
    if ref and title.startswith(f"{ref} · "):  # the store's title already carries the ref
        title = title[len(ref) + 3 :]

    row = Text()
    row.append(f"{marker} ", style=style or "dim")
    if ref:
        row.append(f"{ref} ", style=style or "bold")
    row.append(title[:32], style=style)
    row.append(f"  {worker.status.value}", style=style or "dim")
    reason = item.reason if item is not None else worker.waiting_for
    if reason:
        row.append("  " + _compact(reason, REASON_WIDTH), style="italic")
    tail = [worker.role.value]
    if job is not None:
        tail.append(job.stage.value)
    if repo is not None:
        tail.append(repo.name)
    tail.append(worker.model or "default model")
    row.append("  " + " · ".join(tail), style="dim")
    if worker.pinned:
        row.append("  [pin]", style="bold yellow")
    if worker.active_helpers > 0:
        row.append(f"  {worker.active_helpers} helpers active", style="dim")
    return row


def _render_transcript(messages) -> Text:
    text = Text()
    if not messages:
        text.append("No output yet.", style="dim italic")
        return text
    styles = {
        "user": ("you  ", "bold cyan"),
        "assistant": ("agent", "bold green"),
        "tool": ("tool ", "dim"),
        "system": ("sys  ", "dim yellow"),
    }
    for message in messages:
        prefix, style = styles.get(message.role, (f"{message.role[:5]:<5}", ""))
        if message.role == "tool":
            text.append(f"{prefix} ", style="dim")
            text.append(_compact(message.text, 100) + "\n", style="dim")
            continue
        text.append(f"{prefix} ", style=style)
        body = message.text.rstrip()
        first, _, rest = body.partition("\n")
        text.append(first + "\n")
        if rest:
            for line in rest.splitlines():
                text.append(f"      {line}\n")
        text.append("\n")
    return text


def _row_key_uuid(row_key) -> UUID | None:
    value = getattr(row_key, "value", row_key)
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None
