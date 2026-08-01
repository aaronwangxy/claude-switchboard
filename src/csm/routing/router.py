"""Deterministic routing.

The manager model proposes a route in natural language; this module computes the same
route from state and rules so the application can validate what the model asked for.
Both paths converge on `RouteProposal`, which the session manager executes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from csm.domain.enums import JobStage, WorkerRole, WorkerStatus
from csm.domain.models import Job, Repository, Worker

TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
QUESTION_START = (
    "why", "what", "how", "is", "are", "does", "do", "where", "when", "who", "which", "can",
)

DESTRUCTIVE_PATTERNS = (
    "force push", "force-push", "push --force",
    "delete branch", "delete the branch", "remove branch",
    "reset --hard", "discard", "wipe", "throw away", "blow away",
    "clean up", "cleanup", "tear down", "delete worktree", "remove worktree",
    "merge into", "merge to main",
)

Action = Literal[
    "message_worker",
    "start_workflow",
    "new_job",
    "new_question_worker",
    "confirm_destructive",
    "clarify",
    "status",
]


@dataclass
class RouteProposal:
    action: Action
    reason: str
    worker_id: UUID | None = None
    job_id: UUID | None = None
    repository_id: UUID | None = None
    workflow: str | None = None
    role: WorkerRole = WorkerRole.GENERAL
    writable: bool = False
    title: str = ""
    external_ref: str | None = None
    message: str = ""
    question: str | None = None
    requires_confirmation: bool = False
    #: Priority rule that produced this route (1..6 in Section 6.3 order).
    priority: int = 5

    def describe(self) -> str:
        return f"{self.action}({self.workflow or self.role.value}): {self.reason}"


@dataclass
class RoutingState:
    """The bounded state the router needs. Built by the session manager."""

    repositories: list[Repository] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)
    workers: list[Worker] = field(default_factory=list)
    selected_worker_id: UUID | None = None
    selected_job_id: UUID | None = None
    confirmed: bool = False

    def job(self, job_id: UUID | None) -> Job | None:
        return next((j for j in self.jobs if j.id == job_id), None)

    def worker(self, worker_id: UUID | None) -> Worker | None:
        return next((w for w in self.workers if w.id == worker_id), None)

    def workers_for(self, job_id: UUID) -> list[Worker]:
        return [w for w in self.workers if w.job_id == job_id]

    def primary_writable_worker(self, job_id: UUID) -> Worker | None:
        candidates = [
            w
            for w in self.workers_for(job_id)
            if w.writable and w.status not in (WorkerStatus.STOPPED, WorkerStatus.FAILED)
        ]
        return candidates[-1] if candidates else None


# --------------------------------------------------------------------- helpers


def extract_ticket_ref(text: str) -> str | None:
    match = TICKET_RE.search(text or "")
    return match.group(1) if match else None


def extract_title(text: str, ref: str | None) -> str:
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("#").strip()
        if not line:
            continue
        line = re.sub(r"^(title|summary|subject)\s*:\s*", "", line, flags=re.I)
        if ref:
            line = line.replace(ref, "").strip(" :-–")
        if line:
            return line[:80]
    return (ref or "Untitled request")[:80]


def looks_like_ticket(text: str) -> bool:
    """A pasted ticket: has a ticket id, or is a multi-line block of real substance."""
    if extract_ticket_ref(text):
        return True
    stripped = (text or "").strip()
    return stripped.count("\n") >= 2 and len(stripped) >= 200


def looks_like_question(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped.endswith("?"):
        return False
    first = stripped.split(maxsplit=1)[0].lower().strip("'\"") if stripped.split() else ""
    return first in QUESTION_START


def is_destructive(text: str) -> bool:
    lowered = (text or "").lower()
    return any(pattern in lowered for pattern in DESTRUCTIVE_PATTERNS)


#: Ordered so that more specific phrases win over their prefixes.
WORKFLOW_PHRASES: list[tuple[tuple[str, ...], str]] = [
    (("address these review comments", "address the review comments", "address review comments",
      "review comments:", "here are the review comments"), "address-review-comments"),
    (("rereview", "re-review", "review it again", "review again"), "independent-review"),
    (("restack",), "restack-commits"),
    (("rebase",), "rebase-stack"),
    (("smoke test", "smoke-test"), "smoke-test"),
    (("full verify", "full-verify", "verify everything", "verify"), "full-verify"),
    (("ready to push", "finalize", "finalise", "wrap up"), "finalize-change"),
    (("review this", "review the change", "review it"), "independent-review"),
]


def match_workflow(text: str) -> str | None:
    lowered = (text or "").lower()
    for phrases, workflow in WORKFLOW_PHRASES:
        if any(phrase in lowered for phrase in phrases):
            return workflow
    return None


def resolve_repository(text: str, state: RoutingState, job: Job | None) -> UUID | None:
    """Explicit text > selected job > the only registered repository."""
    lowered = (text or "").lower()
    for repo in state.repositories:
        if re.search(rf"\b{re.escape(repo.name.lower())}\b", lowered):
            return repo.id
    if job is not None:
        return job.repository_id
    if len(state.repositories) == 1:
        return state.repositories[0].id
    return None


# ----------------------------------------------------------------------- route


def resolve_route(text: str, state: RoutingState) -> RouteProposal:
    """Apply the Section 6.3 routing priority to one manager message."""
    text = text or ""

    # 6. Destructive requests always need explicit confirmation, however confident we are.
    if is_destructive(text) and not state.confirmed:
        return RouteProposal(
            action="confirm_destructive",
            reason="This request could discard work; it needs explicit confirmation.",
            job_id=state.selected_job_id,
            worker_id=state.selected_worker_id,
            message=text,
            requires_confirmation=True,
            question="Confirm this destructive operation? Reply 'yes, confirm' to proceed.",
            priority=6,
        )

    ref = extract_ticket_ref(text)
    workflow = match_workflow(text)

    # 1. Explicit ticket reference wins.
    referenced_job = next((j for j in state.jobs if ref and j.external_ref == ref), None)

    # A pasted ticket for an existing job routes to that job rather than duplicating it.
    if looks_like_ticket(text) and not workflow:
        if referenced_job is not None:
            return _route_into_job(text, state, referenced_job, workflow=None, priority=1)
        repo_id = resolve_repository(text, state, None)
        if repo_id is None:
            return RouteProposal(
                action="clarify",
                reason="More than one repository is registered and the ticket does not name one.",
                message=text,
                question="Which registered repository is this ticket for?",
                priority=6,
            )
        return RouteProposal(
            action="new_job",
            reason="New ticket with no matching active job.",
            repository_id=repo_id,
            title=extract_title(text, ref),
            external_ref=ref,
            message=text,
            workflow="plan-feature",
            role=WorkerRole.PLANNER,
            writable=False,
            priority=5,
        )

    if referenced_job is not None:
        return _route_into_job(text, state, referenced_job, workflow, priority=1)

    # 2. Currently selected worker/job.
    selected_job = state.job(state.selected_job_id)
    if selected_job is None and state.selected_worker_id:
        selected = state.worker(state.selected_worker_id)
        selected_job = state.job(selected.job_id) if selected else None

    if workflow and selected_job is not None:
        return _route_into_job(text, state, selected_job, workflow, priority=2)

    if workflow and selected_job is None:
        return RouteProposal(
            action="clarify",
            reason=f"{workflow} needs a job, and none is selected.",
            message=text,
            workflow=workflow,
            question=f"Which job should I run {workflow} on?",
            priority=6,
        )

    # 4/5. Semantic operation type: a question routes to a contextual or read-only worker.
    if looks_like_question(text):
        if selected_job is not None:
            worker = _contextual_worker(state, selected_job)
            if worker is not None:
                return RouteProposal(
                    action="start_workflow",
                    reason="Question about the selected job; its existing worker has the context.",
                    worker_id=worker.id,
                    job_id=selected_job.id,
                    workflow="ask-question",
                    message=text,
                    priority=3,
                )
        repo_id = resolve_repository(text, state, selected_job)
        if repo_id is None:
            return RouteProposal(
                action="clarify",
                reason="No repository context for this question.",
                message=text,
                question="Which repository is this question about?",
                priority=6,
            )
        return RouteProposal(
            action="new_question_worker",
            reason="Standalone question; a read-only question worker needs no worktree.",
            repository_id=repo_id,
            job_id=selected_job.id if selected_job else None,
            role=WorkerRole.QUESTION,
            writable=False,
            workflow="ask-question",
            title=extract_title(text, None),
            message=text,
            priority=4,
        )

    # 3. A plain follow-up goes to the selected worker.
    if state.selected_worker_id and state.worker(state.selected_worker_id):
        return RouteProposal(
            action="message_worker",
            reason="Follow-up for the selected worker.",
            worker_id=state.selected_worker_id,
            job_id=selected_job.id if selected_job else None,
            message=text,
            priority=2,
        )

    if selected_job is not None:
        worker = _contextual_worker(state, selected_job)
        if worker is not None:
            return RouteProposal(
                action="message_worker",
                reason="Follow-up for the selected job's primary worker.",
                worker_id=worker.id,
                job_id=selected_job.id,
                message=text,
                priority=3,
            )

    # 5. Nothing matched: this is unrelated work, so it gets its own job.
    repo_id = resolve_repository(text, state, None)
    if repo_id is None:
        return RouteProposal(
            action="clarify",
            reason="No repository could be resolved for a new job.",
            message=text,
            question="Which registered repository should I use?",
            priority=6,
        )
    return RouteProposal(
        action="new_job",
        reason="Unrelated request; creating its own job rather than polluting an existing worker.",
        repository_id=repo_id,
        title=extract_title(text, ref),
        external_ref=ref,
        message=text,
        workflow="plan-feature",
        role=WorkerRole.PLANNER,
        priority=5,
    )


def _contextual_worker(state: RoutingState, job: Job) -> Worker | None:
    workers = [w for w in state.workers_for(job.id) if w.status != WorkerStatus.STOPPED]
    if not workers:
        return None
    writable = [w for w in workers if w.writable]
    return (writable or workers)[-1]


def _route_into_job(
    text: str, state: RoutingState, job: Job, workflow: str | None, priority: int
) -> RouteProposal:
    """Route a message into an existing job, choosing the right worker for the workflow."""
    if workflow is None:
        worker = _contextual_worker(state, job)
        if worker is None:
            return RouteProposal(
                action="start_workflow",
                reason="Existing job has no live worker; starting a planner for it.",
                job_id=job.id,
                repository_id=job.repository_id,
                workflow="plan-feature",
                role=WorkerRole.PLANNER,
                message=text,
                priority=priority,
            )
        return RouteProposal(
            action="message_worker",
            reason=f"Routed to the existing job {job.external_ref or job.title!r}.",
            worker_id=worker.id,
            job_id=job.id,
            message=text,
            priority=priority,
        )

    from csm.workflows.registry import WorkerMode, get_workflow

    definition = get_workflow(workflow)
    if definition.is_composite:
        return RouteProposal(
            action="start_workflow",
            reason=f"{workflow} is a composite workflow; it runs its own steps for this job.",
            job_id=job.id,
            repository_id=job.repository_id,
            workflow=workflow,
            message=text,
            priority=priority,
        )
    # A workflow declaring `worker: fresh` always gets a brand new independent session,
    # so an independent reviewer never inherits the previous one's context.
    if definition.worker is WorkerMode.FRESH:
        return RouteProposal(
            action="start_workflow",
            reason=f"{workflow} runs on a fresh independent {definition.default_role.value}.",
            job_id=job.id,
            repository_id=job.repository_id,
            workflow=workflow,
            role=definition.default_role,
            writable=False,
            message=text,
            priority=priority,
        )
    worker = state.primary_writable_worker(job.id) if definition.mutates_code else _contextual_worker(state, job)
    if worker is None:
        return RouteProposal(
            action="start_workflow",
            reason=f"{workflow} needs a worker; creating one for this job.",
            job_id=job.id,
            repository_id=job.repository_id,
            workflow=workflow,
            role=definition.default_role,
            writable=definition.mutates_code,
            message=text,
            priority=priority,
        )
    return RouteProposal(
        action="start_workflow",
        reason=f"{workflow} on the job's existing {worker.role.value} worker.",
        worker_id=worker.id,
        job_id=job.id,
        workflow=workflow,
        message=text,
        priority=priority,
    )


def validate(proposal: RouteProposal, state: RoutingState) -> RouteProposal:
    """Reject a route -- including one a model proposed -- that violates an invariant."""
    from csm.workflows.registry import WorkflowError, validate_for_role

    if proposal.worker_id is not None:
        worker = state.worker(proposal.worker_id)
        if worker is None:
            raise RouteError(f"Worker {proposal.worker_id} does not exist.")
        if worker.status == WorkerStatus.STOPPED:
            raise RouteError(f"Worker {worker.title!r} is stopped and cannot take a message.")
        if proposal.workflow:
            try:
                definition = validate_for_role(proposal.workflow, worker.role)
            except WorkflowError as exc:
                raise RouteError(str(exc)) from exc
            if definition.mutates_code and not worker.writable:
                raise RouteError(
                    f"Workflow {proposal.workflow!r} mutates code but worker {worker.title!r} "
                    "is read-only."
                )
    if proposal.job_id is not None and state.job(proposal.job_id) is None:
        raise RouteError(f"Job {proposal.job_id} does not exist.")
    if proposal.repository_id is not None and not any(
        r.id == proposal.repository_id for r in state.repositories
    ):
        raise RouteError(f"Repository {proposal.repository_id} is not registered.")
    if proposal.action in ("new_job", "new_question_worker") and proposal.repository_id is None:
        raise RouteError("A new job or worker needs a resolved repository.")
    if proposal.requires_confirmation and not state.confirmed:
        proposal.action = "confirm_destructive"
    return proposal


class RouteError(ValueError):
    """A proposed route is not permitted."""


def job_is_open(job: Job) -> bool:
    return job.stage not in (JobStage.COMPLETED, JobStage.FAILED)
