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
_VERB = r"(?:approve|approved|approving|go\s+ahead|looks?\s+good|sounds?\s+good|lgtm|ship\s+it)"
#: The user has to be the one approving. The approval has to open its sentence, after at
#: most an assent lead-in and an optional first-person subject -- so "I approve the plan"
#: and "lgtm" grant, while "Worker output: lgtm from the reviewer" reports someone else's.
_APPROVAL_PHRASE = re.compile(
    r"^\s*(?:(?:yes|yep|ok|okay|sure|right|great|perfect|fine)\b[,.!]?\s*)*"
    rf"(?:(?:i|we)\s+)?{_VERB}\b",
    re.I,
)
#: Words that make an already-anchored approval conditional or deferred. Interrogatives and
#: modals ("should I approve", "could we go ahead") need no entry: they cannot open a
#: sentence in the approval position, and questions are excluded by their question mark.
_NOT_APPROVAL = re.compile(
    r"\b(?:not|n't|never|unless|until|before|after|if|wait|waiting|hold)\b",
    re.I,
)
#: A withheld or deferred instruction anywhere in the message vetoes the whole message.
#: Sentence-scoped negation alone let "The plan looks good. Do not implement yet." grant
#: approval purely because the refusal landed after a full stop rather than a comma.
_MESSAGE_VETO = re.compile(
    r"\b(?:do\s+not|don't|do\s+nt|hold\s+off|not\s+yet|wait|stop|cancel|revert"
    r"|before\s+you|until\s+i|no\s+changes\s+until)\b",
    re.I,
)
#: Quoted text is somebody else reporting an approval, not the user giving one.
_QUOTED = re.compile(r"[\"'‘’“”`]")


class _ApprovalPattern:
    """Approval this user states in their own current message.

    The safety property is *whose* turn the approval appears in, not whether the message
    contains nothing else. Requiring the entire message to be the word "approve" rejected
    ordinary human sign-off like "Yes, I approve the plan. Continue the run.", which left
    the user re-approving a plan the application insisted they had not approved.

    Widening that has its own failure mode, so four rules keep the guard honest: the
    approval has to open its own sentence in the user's voice, any withholding instruction
    vetoes the whole message however it is punctuated, a sentence carrying quotation marks
    is somebody else's approval being relayed, and an interrogative never grants.
    """

    def search(self, text: str) -> bool:
        if _MESSAGE_VETO.search(text):
            return False
        for sentence in _SENTENCE.findall(text):
            stripped = sentence.strip()
            if stripped.endswith("?") or _QUOTED.search(stripped):
                continue
            if _NOT_APPROVAL.search(stripped):
                continue
            if _APPROVAL_PHRASE.match(stripped):
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
