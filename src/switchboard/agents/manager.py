"""Manager contracts and the deterministic offline implementation."""

from __future__ import annotations

import re
from typing import Protocol

from switchboard.agents.snapshots import Exchange
from switchboard.core.session_manager import SessionManager
from switchboard.routing import router

CONFIRM_RE = re.compile(
    r"^\s*(?:yes[, ]+)?(?:confirm(?:ed)?|proceed|do it anyway)\s*[.!]?\s*$", re.I
)
APPROVE_RE = re.compile(
    r"^\s*(?:yes[, ]+)?(?:approve(?: the plan)?|approved|go ahead|looks good|lgtm)\s*[.!]?\s*$",
    re.I,
)


class Manager(Protocol):
    async def handle(self, text: str) -> str: ...


class DeterministicManager:
    """Rule-engine manager used by scripted/offline runs and routing tests."""

    def __init__(self, session_manager: SessionManager) -> None:
        self.sm = session_manager
        self.exchanges: list[Exchange] = []

    async def handle(self, text: str) -> str:
        confirmed = bool(CONFIRM_RE.search(text))
        state = self.sm.routing_state(confirmed=confirmed)
        proposal = router.resolve_route(text, state)
        reply = await self.sm.execute_route(proposal, confirmed=confirmed)
        self.exchanges.append(Exchange(user=text, manager=reply))
        return reply
