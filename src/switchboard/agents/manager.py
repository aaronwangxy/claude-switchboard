"""Manager contracts and the deterministic offline implementation."""

from __future__ import annotations

import re
from typing import Protocol

from switchboard.agents.snapshots import Exchange
from switchboard.core.session_manager import SessionManager
from switchboard.routing import router

#: Destructive operations keep a deliberately narrow confirmation: the whole message has
#: to be the confirmation, so nothing incidental can read as consent to stop or delete.
CONFIRM_RE = re.compile(
    r"^\s*(?:yes[, ]+)?(?:confirm(?:ed)?|proceed|do it anyway)\s*[.!]?\s*$", re.I
)

_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
_APPROVAL_PHRASE = re.compile(
    r"\b(?:i\s+)?(?:approve(?:\s+(?:the|this|that)\s+plan)?|approved|go\s+ahead"
    r"|looks?\s+good|sounds?\s+good|lgtm)\b",
    re.I,
)
#: Words that turn a nearby approval phrase into a question, a condition, or a refusal.
_NOT_APPROVAL = re.compile(
    r"\b(?:not|n't|never|no|unless|until|before|after|if|when|whether|why|how|who"
    r"|should|shall|can|could|would|might|maybe|please\s+ask|waiting)\b",
    re.I,
)


class _ApprovalPattern:
    """Approval stated by the user in their own current message.

    The safety property is *whose* turn the approval appears in, not whether the message
    contains nothing else. Requiring the entire message to be the word "approve" rejected
    ordinary human sign-off like "Yes, I approve the plan. Continue the run.", which left
    the user re-approving a plan the application insisted they had not approved.
    """

    def search(self, text: str) -> bool:
        for sentence in _SENTENCE.findall(text):
            if sentence.strip().endswith("?") or _NOT_APPROVAL.search(sentence):
                continue
            if _APPROVAL_PHRASE.search(sentence):
                return True
        return False


APPROVE_RE = _ApprovalPattern()


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
