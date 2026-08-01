"""A backend-neutral description of entering a worker's existing process.

Production native workers provide a tmux attachment to the exact live process. The legacy
command builder remains only for the deterministic scripted backend's attachment tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class AttachError(RuntimeError):
    """This worker cannot be entered as a session."""


@dataclass(frozen=True)
class Attachment:
    """Everything needed to hand a terminal over to a worker's session."""

    cwd: Path
    session_id: str
    argv: list[str]
    #: What the user should know before taking over, if anything. See `_attach_note`.
    note: str = ""

    @property
    def shell_hint(self) -> str:
        """What the user would have typed to get here on their own."""
        return f"cd {self.cwd} && {' '.join(self.argv)}"


def build_attachment(
    *, cwd: Path, session_id: str | None, executable: str | None = None, note: str = ""
) -> Attachment:
    """Build the scripted backend's synthetic resume command.

    Note what this does *not* reproduce: the resumed session is an ordinary interactive
    Claude, so the tool policy Switchboard gave the worker -- read-only, in particular --
    does not apply to it. That is the point of handing over control, but the caller is
    expected to say so through `note` where it could surprise.
    """
    if not session_id:
        raise AttachError(
            "This worker has no Claude session yet, so there is nothing to resume. "
            "It either never started or is running on the scripted backend."
        )
    if not cwd.exists():
        raise AttachError(f"The worker's working directory {cwd} no longer exists.")
    return Attachment(
        cwd=cwd,
        session_id=session_id,
        argv=[executable or "claude", "--resume", session_id],
        note=note,
    )
