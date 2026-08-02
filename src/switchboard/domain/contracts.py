"""The schemas a worker's structured output is validated into.

The spine is the same for every kind of request, whatever the workflow:

    goal  ->  acceptance criteria  ->  evidence

`Goal` states what the request is trying to achieve and what would establish that it has
been. The other models are the evidence: a verification report for code that must behave a
certain way, a findings report for a question or an investigation, a review report for a
change someone had to judge. A workflow declares which of these it produces, and the
completion gate reads them.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class DecisionRequest(BaseModel):
    id: str
    question: str
    options: list[str] = Field(default_factory=list)
    recommendation: str | None = None
    blocking: bool = True


class CommitSpec(BaseModel):
    order: int
    message: str
    purpose: str = ""


class ImplementationContract(BaseModel):
    """What shape should the solution take?"""

    summary_lines: list[str] = Field(default_factory=list)
    decisions: list[DecisionRequest] = Field(default_factory=list)
    commit_stack: list[CommitSpec] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    base_commit: str = ""
    approved: bool = False

    def blocking_decisions(self) -> list[DecisionRequest]:
        return [d for d in self.decisions if d.blocking]


class AcceptanceCriterion(BaseModel):
    """One thing that must be true before the request counts as satisfied.

    Deliberately not phrased in terms of code behaviour: an implementation's criterion is
    "the dispatcher honours the preference", an investigation's is "the mechanism is
    identified with a file and line", a question's is "the answer names the call site".
    """

    model_config = {"populate_by_name": True}

    id: str
    #: `behavior` is the name this had when criteria only described code.
    statement: str = Field(validation_alias=AliasChoices("statement", "behavior"))
    #: `verification_method` is the name this had for the same reason.
    established_by: str = Field(
        default="", validation_alias=AliasChoices("established_by", "verification_method")
    )
    evidence_required: list[str] = Field(default_factory=list)
    status: Literal["pending", "passed", "failed", "blocked"] = "pending"
    accepted_limitation: str | None = None


class Goal(BaseModel):
    """What this request is trying to achieve, and what would establish that it has been.

    `behavior_contract` is the name this artifact had when only implementation work had
    one; stored rows load unchanged because the field names did not move.
    """

    goal: str = ""
    criteria: list[AcceptanceCriterion] = Field(default_factory=list)


class CommandEvidence(BaseModel):
    command: str
    exit_code: int
    output_excerpt: str = ""


class VerificationEvidence(BaseModel):
    """What proof demonstrates each behavior?"""

    criterion_id: str
    status: Literal["passed", "failed", "not_tested", "blocked"]
    commands: list[CommandEvidence] = Field(default_factory=list)
    observed_behavior: str = ""
    artifacts: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    tested_head: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class VerificationReport(BaseModel):
    tested_head: str = ""
    evidence: list[VerificationEvidence] = Field(default_factory=list)
    scope: Literal["smoke", "full"] = "full"

    @property
    def passed(self) -> bool:
        return bool(self.evidence) and all(
            e.status == "passed" or e.limitations for e in self.evidence
        )


class Finding(BaseModel):
    """One evidenced claim from a question or an investigation."""

    id: str
    claim: str
    #: Where the claim comes from: a file and line, a command and its output, an observed
    #: behaviour. A finding with no evidence is a guess, and is labelled one.
    evidence: str = ""
    confidence: Literal["confirmed", "likely", "speculative"] = "likely"


class FindingsReport(BaseModel):
    """The durable result of asking a Claude something, rather than a lost transcript.

    `answer` is the part a person reads: the root cause, or the direct answer. It is what
    makes this artifact usable as the input to a later job -- a fix worker is given the
    findings, not a summary somebody retyped.
    """

    question: str = ""
    answer: str = ""
    findings: list[Finding] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class ReviewFinding(BaseModel):
    id: str
    severity: Literal["blocking", "important", "minor", "nit"]
    category: str
    description: str
    evidence: str = ""
    recommended_action: str = ""
    reviewed_head: str = ""
    resolved: bool = False


class ReviewReport(BaseModel):
    verdict: Literal["pass", "changes_requested"] = "pass"
    base_commit: str = ""
    reviewed_head: str = ""
    findings: list[ReviewFinding] = Field(default_factory=list)

    def unresolved_blocking(self, blocking_severities: set[str]) -> list[ReviewFinding]:
        return [f for f in self.findings if f.severity in blocking_severities and not f.resolved]


class CommentResolution(BaseModel):
    original_comment: str
    classification: Literal[
        "valid", "partially_valid", "invalid", "already_addressed", "needs_human_decision"
    ]
    reasoning: str
    action_taken: str | None = None
    commit: str | None = None
    verification_required: list[str] = Field(default_factory=list)


class CommentResolutionReport(BaseModel):
    resolutions: list[CommentResolution] = Field(default_factory=list)


class ProposedStep(BaseModel):
    """One step of a proposed workflow, named after an existing workflow."""

    workflow: str
    when: str = "always"


class WorkflowProposal(BaseModel):
    """A reusable workflow a miner believes the user keeps performing by hand.

    A proposal is inert. It becomes a workflow only when the user accepts it, which is
    what keeps mining from quietly changing how future requests are routed.
    """

    name: str
    description: str
    steps: list[ProposedStep] = Field(default_factory=list)
    worker: Literal["fresh", "existing", "auto"] = "auto"
    evidence: str = ""
    rationale: str = ""


class WorkflowProposals(BaseModel):
    proposals: list[WorkflowProposal] = Field(default_factory=list)


def extract_json_block(text: str) -> dict | None:
    """Pull the last fenced JSON object out of agent output.

    Workers are instructed to emit their structured artifact in a ```json fence.
    Parsing is deterministic application code; the model never writes the database.
    """
    candidates = _FENCE.findall(text or "")
    for raw in reversed(candidates):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
