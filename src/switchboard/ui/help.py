"""The help screen and the single source of truth for key bindings.

Every binding the application defines is described here, including the two places where
Textual already owns the obvious key and this application had to pick a neighbour.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

#: (keys, what it does). Rendered by the help screen and used for the footer.
KEY_BINDINGS: list[tuple[str, str]] = [
    ("Ctrl+N", "Focus the manager input"),
    ("Ctrl+J", "Select the next worker"),
    ("Ctrl+K", "Select the previous worker"),
    ("Ctrl+Space", "Jump to the next attention item"),
    ("Ctrl+P", "Pin / unpin the selected worker"),
    ("Ctrl+S", "Snooze the selected worker for 30 minutes"),
    ("Ctrl+A", "Toggle auto-advance"),
    ("Ctrl+O", "Interrupt the selected worker"),
    ("Ctrl+E", "Enter the selected Manager or worker session"),
    ("Escape", "Return focus to the worker list"),
    ("?", "Show this help"),
    ("Enter", "Enter the highlighted session, or submit the Manager input"),
    ("Ctrl+Q", "Quit"),
]

#: Where a documented default had to move because Textual reserves the key.
SUBSTITUTIONS: list[str] = [
    "Ctrl+O interrupts, not Ctrl+C: Textual reserves Ctrl+C for quit (and for copying a "
    "selection inside a screen), so it can never reach the application.",
    "Ctrl+P pins the selected worker. Textual normally opens its command palette on "
    "Ctrl+P, so this application disables the command palette (ENABLE_COMMAND_PALETTE = "
    "False) to keep the documented binding.",
    "Ctrl+A (auto-advance) and Ctrl+K (previous worker) are application bindings with "
    "priority, so inside a text input they no longer mean 'go to line start' and 'delete "
    "to end of line'. Home/End and Ctrl+U still work.",
    "Ctrl+Space is delivered by some terminals as Ctrl+@; both are bound.",
]

HELP_TEXT = "\n".join(
    ["[b]Switchboard[/b]", ""]
    + [f"  [b]{keys:<12}[/b] {description}" for keys, description in KEY_BINDINGS]
    + [
        "",
        "[b]Panes[/b]",
        "  Manager (top left)   one high-level input; native Claude owns its conversation.",
        "  Sessions (bottom)    Manager and workers. ! needs you   ● working   ✓ idle/done",
        "  Detail (right)       workflow, ownership, dependencies, worktree, and evidence.",
        "                       Highlight any session and press Enter to open exact Claude.",
        "",
        "[b]Substituted keys[/b]",
    ]
    + [f"  - {line}" for line in SUBSTITUTIONS]
    + [
        "",
        "[b]Auto-advance[/b]",
        "  When it is on, finishing or blocking a worker opens the highest-priority",
        "  attention item. It never switches away while you are typing in an input, and",
        "  never leaves a pinned worker. When nothing needs you, focus returns to the",
        "  manager input.",
        "",
        "  Press Escape or ? to close this help.",
    ]
)


class HelpScreen(ModalScreen[None]):
    """A modal listing every binding, including the substituted ones."""

    BINDINGS = [
        Binding("escape", "dismiss_help", "Close", priority=True),
        Binding("question_mark", "dismiss_help", "Close"),
        Binding("q", "dismiss_help", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-body"):
            yield Static(HELP_TEXT, id="help-text")

    def action_dismiss_help(self) -> None:
        self.dismiss(None)
