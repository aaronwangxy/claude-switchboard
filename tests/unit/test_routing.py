"""Routing: the Section 6.3 priority order, ticket intake, and route validation."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from csm.domain.enums import JobStage, WorkerRole, WorkerStatus
from csm.domain.models import Job, Repository, Worker
from csm.routing.router import (
    RouteError,
    RoutingState,
    extract_ticket_ref,
    extract_title,
    looks_like_question,
    looks_like_ticket,
    resolve_route,
    validate,
)

TICKET = """ENG-421 Notification preferences

Users need per-channel notification preferences that persist across restarts.
The dispatcher must honour them. Acceptance: preferences survive a restart.
"""


@pytest.fixture
def repo() -> Repository:
    return Repository(name="alpha", root_path=Path("/tmp/alpha"), default_branch="main")


@pytest.fixture
def other_repo() -> Repository:
    return Repository(name="beta", root_path=Path("/tmp/beta"), default_branch="main")


def make_job(repo: Repository, ref: str | None = "ENG-421") -> Job:
    return Job(title="Notification preferences", external_ref=ref, repository_id=repo.id)


def make_worker(repo: Repository, job: Job | None, role: WorkerRole, writable: bool) -> Worker:
    return Worker(
        job_id=job.id if job else None,
        title=f"{role.value} worker",
        role=role,
        status=WorkerStatus.IDLE,
        repository_id=repo.id,
        cwd=repo.root_path,
        writable=writable,
    )


# ------------------------------------------------------------------ extraction


def test_extracts_ticket_identifier_and_title():
    assert extract_ticket_ref(TICKET) == "ENG-421"
    assert extract_title(TICKET, "ENG-421") == "Notification preferences"


def test_untagged_multiline_paste_still_reads_as_a_ticket():
    assert looks_like_ticket(TICKET)
    assert looks_like_ticket("Fix the cache") is False


def test_question_detection_needs_a_question_word_and_mark():
    assert looks_like_question("Is this cache shared between requests?")
    assert looks_like_question("Rebase this") is False
    assert looks_like_question("Delete the branch?") is False  # not a question word


# --------------------------------------------------------------- ticket intake


def test_pasted_ticket_creates_a_job_and_a_read_only_planner(repo):
    state = RoutingState(repositories=[repo])
    route = resolve_route(TICKET, state)
    assert route.action == "new_job"
    assert route.external_ref == "ENG-421"
    assert route.title == "Notification preferences"
    assert route.repository_id == repo.id
    assert route.workflow == "complete-ticket"
    assert route.writable is False


def test_pasted_ticket_for_an_active_job_routes_there_instead_of_duplicating(repo):
    job = make_job(repo)
    worker = make_worker(repo, job, WorkerRole.PLANNER, writable=False)
    state = RoutingState(repositories=[repo], jobs=[job], workers=[worker])
    route = resolve_route(TICKET, state)
    assert route.action == "message_worker"
    assert route.job_id == job.id
    assert route.worker_id == worker.id


def test_ambiguous_repository_asks_exactly_one_question(repo, other_repo):
    state = RoutingState(repositories=[repo, other_repo])
    route = resolve_route(TICKET, state)
    assert route.action == "clarify"
    assert route.question and route.question.count("?") == 1


def test_repository_named_in_the_ticket_resolves_the_ambiguity(repo, other_repo):
    state = RoutingState(repositories=[repo, other_repo])
    route = resolve_route(TICKET + "\nRepo: beta\n", state)
    assert route.action == "new_job"
    assert route.repository_id == other_repo.id


# --------------------------------------------------------- operation shorthands


@pytest.mark.parametrize(
    ("text", "workflow"),
    [
        ("Rebase this stack.", "rebase-stack"),
        ("Run another smoke test.", "smoke-test"),
        ("Rereview it.", "independent-review"),
        ("Verify only the auth flow again.", "full-verify"),
        ("Address these review comments: the cache is shared.", "address-review-comments"),
        ("Restack the commits.", "restack-commits"),
    ],
)
def test_shorthands_route_to_the_selected_job_and_right_workflow(repo, text, workflow):
    job = make_job(repo)
    impl = make_worker(repo, job, WorkerRole.IMPLEMENTER, writable=True)
    state = RoutingState(
        repositories=[repo], jobs=[job], workers=[impl], selected_worker_id=impl.id
    )
    route = resolve_route(text, state)
    assert route.action == "start_workflow"
    assert route.workflow == workflow
    assert route.job_id == job.id


def test_rereview_always_starts_a_fresh_independent_reviewer(repo):
    job = make_job(repo)
    impl = make_worker(repo, job, WorkerRole.IMPLEMENTER, writable=True)
    old_reviewer = make_worker(repo, job, WorkerRole.REVIEWER, writable=False)
    state = RoutingState(
        repositories=[repo],
        jobs=[job],
        workers=[impl, old_reviewer],
        selected_worker_id=impl.id,
    )
    route = resolve_route("Rereview it after the rebase.", state)
    assert route.workflow == "independent-review"
    assert route.worker_id is None, "a rereview must not reuse the previous reviewer"
    assert route.role == WorkerRole.REVIEWER
    assert route.writable is False


def test_mutating_workflow_targets_the_jobs_writable_worker(repo):
    job = make_job(repo)
    planner = make_worker(repo, job, WorkerRole.PLANNER, writable=False)
    impl = make_worker(repo, job, WorkerRole.IMPLEMENTER, writable=True)
    state = RoutingState(
        repositories=[repo], jobs=[job], workers=[planner, impl], selected_worker_id=planner.id
    )
    route = resolve_route("Rebase this stack.", state)
    assert route.worker_id == impl.id


def test_workflow_without_a_job_asks_which_job(repo):
    state = RoutingState(repositories=[repo])
    route = resolve_route("Run another smoke test.", state)
    assert route.action == "clarify"
    assert "smoke-test" in (route.question or "")


# -------------------------------------------------------------------- questions


def test_standalone_question_creates_a_read_only_worker_with_no_worktree(repo):
    state = RoutingState(repositories=[repo])
    route = resolve_route("Is this cache shared between requests?", state)
    assert route.action == "new_question_worker"
    assert route.writable is False
    assert route.role == WorkerRole.QUESTION


def test_question_about_the_selected_job_reuses_its_contextual_worker(repo):
    job = make_job(repo)
    impl = make_worker(repo, job, WorkerRole.IMPLEMENTER, writable=True)
    state = RoutingState(
        repositories=[repo], jobs=[job], workers=[impl], selected_worker_id=impl.id
    )
    route = resolve_route("Why is this cache shared?", state)
    assert route.worker_id == impl.id
    assert route.workflow == "ask-question"


# ------------------------------------------------------------------ new vs. old


def test_unrelated_request_creates_its_own_job_rather_than_polluting_a_worker(repo):
    job = make_job(repo)
    impl = make_worker(repo, job, WorkerRole.IMPLEMENTER, writable=True)
    state = RoutingState(repositories=[repo], jobs=[job], workers=[impl])
    unrelated = (
        "ENG-999 Rewrite the billing exporter\n\n"
        "The nightly billing exporter times out on large tenants and needs a streaming\n"
        "rewrite so that memory stays flat. Acceptance: exports finish under ten minutes.\n"
    )
    route = resolve_route(unrelated, state)
    assert route.action == "new_job"
    assert route.external_ref == "ENG-999"
    assert route.job_id is None


def test_plain_follow_up_goes_to_the_selected_worker(repo):
    job = make_job(repo)
    impl = make_worker(repo, job, WorkerRole.IMPLEMENTER, writable=True)
    state = RoutingState(
        repositories=[repo], jobs=[job], workers=[impl], selected_worker_id=impl.id
    )
    route = resolve_route("Use a dataclass instead.", state)
    assert route.action == "message_worker"
    assert route.worker_id == impl.id


# ------------------------------------------------------------------ destructive


@pytest.mark.parametrize(
    "text",
    [
        "Clean up that worker.",
        "Force push the branch.",
        "Delete the branch when you are done.",
        "git reset --hard and start over",
    ],
)
def test_destructive_requests_require_explicit_confirmation(repo, text):
    job = make_job(repo)
    impl = make_worker(repo, job, WorkerRole.IMPLEMENTER, writable=True)
    state = RoutingState(
        repositories=[repo], jobs=[job], workers=[impl], selected_worker_id=impl.id
    )
    route = resolve_route(text, state)
    assert route.action == "confirm_destructive"
    assert route.requires_confirmation


def test_confirmed_destructive_request_is_allowed_through(repo):
    job = make_job(repo)
    impl = make_worker(repo, job, WorkerRole.IMPLEMENTER, writable=True)
    state = RoutingState(
        repositories=[repo],
        jobs=[job],
        workers=[impl],
        selected_worker_id=impl.id,
        confirmed=True,
    )
    route = resolve_route("Clean up that worker.", state)
    assert route.action != "confirm_destructive"


# ------------------------------------------------------------------ validation


def test_validate_rejects_a_mutating_workflow_on_a_read_only_worker(repo):
    job = make_job(repo)
    reviewer = make_worker(repo, job, WorkerRole.REVIEWER, writable=False)
    state = RoutingState(repositories=[repo], jobs=[job], workers=[reviewer])
    route = resolve_route("Rebase this stack.", RoutingState(
        repositories=[repo], jobs=[job], workers=[reviewer], selected_worker_id=reviewer.id
    ))
    route.worker_id = reviewer.id
    route.workflow = "rebase-stack"
    with pytest.raises(RouteError, match="read-only|cannot run"):
        validate(route, state)


def test_validate_rejects_an_unknown_worker(repo):
    state = RoutingState(repositories=[repo])
    route = resolve_route("Use a dataclass instead.", state)
    route.action = "message_worker"
    route.worker_id = uuid4()
    with pytest.raises(RouteError, match="does not exist"):
        validate(route, state)


def test_validate_rejects_a_stopped_worker(repo):
    job = make_job(repo)
    impl = make_worker(repo, job, WorkerRole.IMPLEMENTER, writable=True)
    impl.status = WorkerStatus.STOPPED
    state = RoutingState(repositories=[repo], jobs=[job], workers=[impl])
    route = resolve_route(
        "Use a dataclass instead.",
        RoutingState(repositories=[repo], jobs=[job], workers=[impl], selected_worker_id=impl.id),
    )
    with pytest.raises(RouteError, match="stopped"):
        validate(route, state)


def test_validate_rejects_an_unregistered_repository(repo, other_repo):
    state = RoutingState(repositories=[repo])
    route = resolve_route(TICKET, RoutingState(repositories=[other_repo]))
    with pytest.raises(RouteError, match="not registered"):
        validate(route, state)


def test_completed_jobs_do_not_capture_a_new_ticket(repo):
    job = make_job(repo)
    job.stage = JobStage.COMPLETED
    state = RoutingState(repositories=[repo], jobs=[job])
    route = resolve_route(TICKET, state)
    # The ref still matches, so it routes to that job rather than duplicating it;
    # with no live worker it restarts planning instead of messaging a dead session.
    assert route.action == "start_workflow"
    assert route.job_id == job.id
