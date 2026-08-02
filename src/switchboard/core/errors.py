"""The refusal every orchestration module raises.

It lives on its own so the modules `SessionManager` delegates to can refuse without
importing `SessionManager` back.
"""

from __future__ import annotations

from uuid import UUID


class SessionManagerError(RuntimeError):
    """An operation was refused because it would violate an application invariant."""

    def __init__(self, *args: object, worker_id: UUID | None = None) -> None:
        super().__init__(*args)
        #: Set when the refusal left a real worker behind, so a caller can still own it.
        self.worker_id = worker_id
