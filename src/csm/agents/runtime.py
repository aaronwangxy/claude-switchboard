"""Locating the Claude executable.

The command is configuration rather than a literal `claude`, so a wrapper can be used
instead. CSM only decides which executable to launch; the parent environment is always
inherited, so whatever that wrapper configures still applies.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


class ClaudeRuntimeError(RuntimeError):
    """The configured Claude executable cannot be used."""


def claude_cli_path(configured: str | None) -> Path | None:
    """Resolve the configured executable, or None to let the SDK find its own.

    A shell alias or shell function is not an executable and cannot be launched; the
    error says so, because that is the mistake this is most likely to catch.
    """
    if not configured:
        return None
    candidate = Path(configured).expanduser()
    found = str(candidate) if candidate.is_absolute() else shutil.which(configured)
    if not found or not os.access(found, os.X_OK):
        raise ClaudeRuntimeError(
            f"Configured Claude executable {configured!r} was not found on PATH or is not "
            "executable. A shell alias or shell function cannot be launched directly -- "
            "point claude.executable at a real executable wrapper."
        )
    return Path(found)
