"""The orchestration service.

UI actions call this; this emits events; persistence, status, and the attention queue
follow from those events. Every Git and worktree invariant is enforced here in ordinary
Python -- never by asking a model to behave.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import yaml
from pydantic import ValidationError

from switchboard.agents.attach import AttachError, Attachment, build_attachment
from switchboard.agents.backend import WorkerBackend, WorkerEvent, WorkerSpec
from switchboard.agents.prompts import PROMPT_POLICY_VERSION, compose_worker_prompt
from switchboard.config import Config, user_workflows_dir
from switchboard.core.runs import condition_holds, has_blocking_decisions
from switchboard.core.transitions import assert_worker_transition
from switchboard.domain import events as ev
from switchboard.domain.contracts import (
    BehaviorContract,
    CommentResolutionReport,
    ImplementationContract,
    ReviewReport,
    VerificationReport,
    WorkflowProposal,
    WorkflowProposals,
    extract_json_block,
)
from switchboard.domain.enums import (
    READ_ONLY_ROLES,
    ArtifactType,
    AttentionKind,
    JobStage,
    RunStatus,
    RuntimeAgentKind,
    RuntimeOwner,
    RuntimeProcessState,
    Verbosity,
    WorkerRole,
    WorkerStatus,
)
from switchboard.domain.models import (
    Artifact,
    AttentionItem,
    Decision,
    Event,
    Job,
    Repository,
    RuntimeInstance,
    TranscriptMessage,
    Worker,
    WorkflowExecution,
    WorkflowRun,
    Worktree,
    now,
)
from switchboard.gitops import runner
from switchboard.gitops.runner import GitError
from switchboard.gitops.worktrees import CleanupDecision, WorktreeSafetyError, WorktreeService, slug
from switchboard.routing import router
from switchboard.routing.router import RouteError, RouteProposal, RoutingState
from switchboard.storage.store import Store
from switchboard.workflows.freshness import (
    BEHAVIORAL_ARTIFACTS,
    CodeChange,
    GitSnapshot,
    artifacts_invalidated_by,
    classify_change,
    is_fresh,
    relineage,
)
from switchboard.workflows.registry import (
    REPO_WORKFLOW_DIR,
    Approval,
    WorkerMode,
    WorkflowDefinition,
    WorkflowError,
    WorkflowStep,
    builtin_names,
    get_workflow,
    reload_workflows,
    render_template,
    validate_for_role,
    workflow_names,
)

log = logging.getLogger(__name__)


class SessionManagerError(RuntimeError):
    """An operation was refused because it would violate an application invariant."""


#: Fallback for a worker started with a role but no workflow (a bare `create_worker`).
ROLE_ARTIFACTS: dict[WorkerRole, frozenset[ArtifactType]] = {
    WorkerRole.PLANNER: frozenset({ArtifactType.IMPLEMENTATION_CONTRACT}),
    WorkerRole.VERIFIER: frozenset({ArtifactType.VERIFICATION}),
    WorkerRole.REVIEWER: frozenset({ArtifactType.REVIEW}),
    WorkerRole.REVIEW_COMMENTS: frozenset({ArtifactType.COMMENT_RESOLUTIONS}),
}


@dataclass
class ReadyToPushReport:
    ready: bool
    blockers: list[str] = field(default_factory=list)
    blurb: str = ""


@dataclass
class CleanupResult:
    performed: bool
    decision: CleanupDecision


class SessionManager:
    def __init__(
        self,
        store: Store,
        backend: WorkerBackend,
        config: Config,
        worktrees: WorktreeService,
    ) -> None:
        self.store = store
        self.backend = backend
        self.config = config
        self.worktrees = worktrees
        self.selected_worker_id: UUID | None = None
        self.auto_advance: bool = True
        self.verbosity: dict[UUID, Verbosity] = {}
        self._pumps: dict[UUID, asyncio.Task] = {}
        self._listeners: list[Callable[[Event], None]] = []
        #: Strong references to in-flight run advances, so they are not garbage collected.
        self._background: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ events

    def subscribe(self, listener: Callable[[Event], None]) -> None:
        self._listeners.append(listener)

    def emit(
        self,
        kind: str,
        *,
        job_id: UUID | None = None,
        worker_id: UUID | None = None,
        summary: str = "",
        payload: dict | None = None,
    ) -> Event:
        event = Event(
            kind=kind, job_id=job_id, worker_id=worker_id, summary=summary, payload=payload or {}
        )
        self.store.add_event(event)
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # a broken listener must not break orchestration
                log.exception("event listener failed for %s", kind)
        return event

    # ------------------------------------------------------------ repositories

    def register_repository(self, path: Path | str, name: str | None = None) -> Repository:
        candidate = Path(path).expanduser()
        if not candidate.exists():
            raise SessionManagerError(f"{candidate} does not exist.")
        if not runner.is_git_repository(candidate):
            raise SessionManagerError(f"{candidate} is not a Git repository.")
        root = runner.repo_toplevel(candidate)
        existing = self.store.get_repository_by_path(root)
        if existing:
            return existing
        repo = Repository(
            name=name or root.name,
            root_path=root,
            default_branch=runner.default_branch(root),
        )
        self.store.add_repository(repo)
        self.reload_workflows()
        return repo

    def list_repositories(self) -> list[Repository]:
        return self.store.list_repositories()

    def reload_workflows(self) -> list[str]:
        """Reload built-in, user, and repository-local workflows. Returns any problems.

        A registered repository may carry its own workflows in `.switchboard/workflows`, so a
        team convention travels with the repository rather than with this machine.
        """
        directories = [
            repo.root_path / REPO_WORKFLOW_DIR
            for repo in self.store.list_repositories()
            if (repo.root_path / REPO_WORKFLOW_DIR).is_dir()
        ]
        return reload_workflows(directories)

    # -------------------------------------------------------------------- jobs

    def create_job(
        self,
        title: str,
        repository_id: UUID,
        external_ref: str | None = None,
        base_ref: str | None = None,
        ticket_text: str = "",
    ) -> Job:
        repo = self.store.get_repository(repository_id)
        if repo is None:
            raise SessionManagerError(f"Repository {repository_id} is not registered.")
        job = Job(
            title=title,
            external_ref=external_ref,
            repository_id=repository_id,
            base_ref=base_ref or repo.default_branch,
            ticket_text=ticket_text,
        )
        self.store.save_job(job)
        return job

    def update_job_stage(self, job: Job, stage: JobStage) -> Job:
        job.stage = stage
        job.updated_at = now()
        return self.store.save_job(job)

    # ----------------------------------------------------------------- workers

    async def create_worker(
        self,
        *,
        role: WorkerRole,
        title: str,
        prompt: str,
        job_id: UUID | None = None,
        repository_id: UUID | None = None,
        writable: bool | None = None,
        model: str | None = None,
        workflow: str | None = None,
    ) -> Worker:
        job = self.store.get_job(job_id) if job_id else None
        repo_id = repository_id or (job.repository_id if job else None)
        if repo_id is None:
            raise SessionManagerError("A worker needs a repository.")
        repo = self.store.get_repository(repo_id)
        if repo is None:
            raise SessionManagerError(f"Repository {repo_id} is not registered.")

        # Read-only by default for reviewer/question/planner/verifier roles.
        if writable is None:
            writable = role not in READ_ONLY_ROLES

        worker = Worker(
            job_id=job.id if job else None,
            title=title,
            role=role,
            repository_id=repo.id,
            cwd=repo.root_path,
            writable=writable,
            model=model or self.config.model_for_role(role.value),
            workflow=workflow,
            prompt_policy_version=PROMPT_POLICY_VERSION,
        )

        if writable:
            worktree = self._allocate_worktree(repo, job, worker)
            worker.worktree_id = worktree.id
            worker.cwd = worktree.path
        elif job is not None:
            # Read-only workers observe the job's writable worktree when one exists, so a
            # reviewer or verifier sees the change under review without owning it.
            worker.cwd = self._job_inspection_path(job) or repo.root_path

        self.store.save_worker(worker)
        self.store.save_runtime(self._new_runtime(worker, generation=1))
        self.emit(
            ev.WORKER_CREATED,
            job_id=worker.job_id,
            worker_id=worker.id,
            summary=f"{role.value} worker {title!r} in {worker.cwd}",
        )
        await self._start_backend(worker, prompt)
        return worker

    def _allocate_worktree(self, repo: Repository, job: Job | None, worker: Worker) -> Worktree:
        """Give a writable worker its own fresh worktree.

        Every writable worker gets a distinct path (the path embeds the worker id), so
        two writers in one repository can never land in the same tree. The ownership
        assertion below is the belt-and-braces check on that: if a record for this path
        already exists under another owner, refuse rather than take it over.
        """
        base_ref = job.base_ref if job else repo.default_branch
        try:
            worktree = self.worktrees.create_worktree(repo, job, worker, base_ref)
        except (GitError, WorktreeSafetyError) as exc:
            raise SessionManagerError(f"Could not create a worktree: {exc}") from exc
        for existing in self.store.list_worktrees(repo.id):
            if existing.path == worktree.path:
                WorktreeService.assert_single_writable_owner(
                    existing.id, existing.owner_worker_id, worker.id
                )
        return self.store.save_worktree(worktree)

    def _job_inspection_path(self, job: Job) -> Path | None:
        for worker in self.store.list_workers(job.id):
            if worker.writable and worker.worktree_id:
                worktree = self.store.get_worktree(worker.worktree_id)
                if worktree and worktree.path.exists():
                    return worktree.path
        return None

    async def _start_backend(
        self, worker: Worker, prompt: str, resume: bool = False, adopt: bool = False
    ) -> None:
        runtime = self.store.current_runtime(worker.id)
        if runtime is None:
            raise SessionManagerError(f"Worker {worker.title!r} has no runtime instance.")
        runtime.process_state = RuntimeProcessState.STARTING
        runtime.updated_at = now()
        self.store.save_runtime(runtime)
        spec = WorkerSpec(
            worker_id=worker.id,
            role=worker.role.value,
            cwd=worker.cwd,
            system_prompt_append=compose_worker_prompt(
                worker.role,
                self.config,
                writable=worker.writable,
                verbosity=self.verbosity.get(worker.id, Verbosity.CONCISE),
                workflow_policy=self._workflow_policy(worker.workflow),
            ),
            initial_prompt=prompt,
            model=worker.model,
            writable=worker.writable,
            setting_sources=list(self.config.setting_sources),
            resume_session_id=worker.session_id if resume else None,
            max_helpers=self.config.subagents.max_concurrent_per_worker,
            claude_executable=self.config.claude.executable,
            env=dict(self.config.claude.env),
            runtime_id=runtime.id,
            runtime_generation=runtime.generation,
        )
        if prompt:
            self._record(worker, "user", prompt)
        try:
            handle = (
                await self.backend.adopt(spec)
                if adopt
                else await self.backend.resume(spec)
                if resume
                else await self.backend.start(spec)
            )
        except Exception as exc:
            self._set_status(worker, WorkerStatus.FAILED, waiting_for=f"Backend error: {exc}")
            self.emit(
                ev.WORKER_FAILED, worker_id=worker.id, job_id=worker.job_id, summary=str(exc)
            )
            raise SessionManagerError(f"Could not start worker {worker.title!r}: {exc}") from exc
        if handle.session_id:
            worker.session_id = handle.session_id
            runtime.claude_session_id = handle.session_id
        runtime.process_state = (
            RuntimeProcessState.TURN_ACTIVE if prompt else RuntimeProcessState.READY
        )
        runtime.updated_at = now()
        self.store.save_runtime(runtime)
        self._set_status(worker, WorkerStatus.WORKING)
        self.emit(
            ev.WORKER_RESUMED if resume else ev.WORKER_STARTED,
            worker_id=worker.id,
            job_id=worker.job_id,
            summary=worker.title,
        )
        self._pumps[worker.id] = asyncio.create_task(self._pump(worker.id))

    def _new_runtime(self, worker: Worker, *, generation: int) -> RuntimeInstance:
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "cwd": str(worker.cwd),
                    "model": worker.model,
                    "writable": worker.writable,
                    "setting_sources": self.config.setting_sources,
                    "executable": self.config.claude.executable,
                    "env_keys": sorted(self.config.claude.env),
                    "prompt_policy_version": worker.prompt_policy_version,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return RuntimeInstance(
            agent_id=worker.id,
            agent_kind=RuntimeAgentKind.WORKER,
            generation=generation,
            backend=type(self.backend).__name__,
            claude_session_id=worker.session_id,
            launch_fingerprint=fingerprint,
        )

    def _set_runtime_state(
        self, worker_id: UUID, state: RuntimeProcessState
    ) -> RuntimeInstance | None:
        runtime = self.store.current_runtime(worker_id)
        if runtime is None:
            return None
        runtime.process_state = state
        runtime.updated_at = now()
        return self.store.save_runtime(runtime)

    def _workflow_policy(self, workflow: str | None) -> str | None:
        definition = self._definition(workflow)
        if definition is None:
            return None
        return f"Current workflow: {definition.name}. {definition.description.strip()}"

    # --------------------------------------------------------------- messaging

    async def send(self, worker_id: UUID, message: str) -> None:
        worker = self._require_worker(worker_id)
        runtime = self.store.current_runtime(worker_id)
        if runtime is not None and runtime.owner is RuntimeOwner.HUMAN:
            # The user's own client is writing this session. A second writer would
            # interleave turns in one session file.
            raise SessionManagerError(
                f"You are attached to {worker.title!r}. Talk to it in that session, or "
                "leave it before sending from here."
            )
        if worker.status in (WorkerStatus.STOPPED, WorkerStatus.DISCONNECTED):
            raise SessionManagerError(
                f"Worker {worker.title!r} is {worker.status.value}; start a replacement instead."
            )
        self._record(worker, "user", message)
        self._resolve_attention(worker)
        self._set_status(worker, WorkerStatus.WORKING, waiting_for=None)
        self._snapshot_before_change(worker)
        self._set_runtime_state(worker.id, RuntimeProcessState.TURN_ACTIVE)
        self._unpause_run_of(worker)
        await self.backend.send(worker_id, message)

    def _unpause_run_of(self, worker: Worker) -> None:
        """Answering the worker a run stopped on puts the run back in flight."""
        run = self.store.run_for_worker(worker.id)
        if run is not None and run.status in (RunStatus.AWAITING_APPROVAL, RunStatus.BLOCKED):
            run.status = RunStatus.RUNNING
            run.detail = ""
            run.updated_at = now()
            self.store.save_run(run)

    async def start_workflow(
        self,
        workflow_name: str,
        *,
        job_id: UUID | None = None,
        target_worker_id: UUID | None = None,
        request: str = "",
    ) -> Worker:
        """Run a workflow on an existing worker, or create the worker it requires."""
        definition = get_workflow(workflow_name)
        if definition.is_composite:
            raise SessionManagerError(
                f"{definition.name} is a composite workflow; run its steps instead."
            )
        job = self.store.get_job(job_id) if job_id else None
        worker = self.store.get_worker(target_worker_id) if target_worker_id else None
        self._assert_prerequisites(definition, job)

        if worker is not None:
            validate_for_role(definition.name, worker.role)
            if definition.mutates_code and not worker.writable:
                raise SessionManagerError(
                    f"{definition.name} mutates code but {worker.title!r} is read-only."
                )
            worker.workflow = definition.name
            self.store.save_worker(worker)
            prompt = self._render(definition, job, request)
            self._note_execution(job, worker, definition.name)
            self._adopt_into_run(job, worker, definition)
            await self.send(worker.id, prompt)
            self._advance_stage(job, definition)
            return worker

        if job is None:
            raise SessionManagerError(f"{definition.name} needs a job or a target worker.")
        prompt = self._render(definition, job, request)
        worker = await self.create_worker(
            role=definition.default_role,
            title=f"{job.external_ref or job.title} · {definition.default_role.value}",
            prompt=prompt,
            job_id=job.id,
            writable=definition.mutates_code,
            workflow=definition.name,
        )
        self._note_execution(job, worker, definition.name)
        self._snapshot_before_change(worker)
        self._advance_stage(job, definition)
        self._adopt_into_run(job, worker, definition)
        return worker

    def _adopt_into_run(self, job: Job | None, worker: Worker, definition: WorkflowDefinition) -> None:
        """Let a manually started workflow count as the run's current step.

        Without this, invoking the step a paused run was about to run anyway would make
        the run start a second worker for it once the user resumed.
        """
        if job is None:
            return
        run = self.store.active_run(job.id)
        if run is None or run.current_worker_id is not None:
            return
        composite = self._definition(run.workflow)
        if composite is None or run.step_index >= len(composite.steps):
            return
        if composite.steps[run.step_index].workflow != definition.name:
            return
        run.iterations[str(run.step_index)] = run.iterations.get(str(run.step_index), 0) + 1
        run.current_worker_id = worker.id
        run.status = RunStatus.RUNNING
        run.updated_at = now()
        self.store.save_run(run)

    # ------------------------------------------------------- composite workflows

    def resolve_profile(self, repository_id: UUID | None, job: Job | None = None) -> str:
        """Which composite workflow a job should follow.

        Precedence: the job's stored profile, then the repository preference, then the
        user's configured default. A one-off instruction beats all of them by naming a
        workflow explicitly, which never reaches this method.
        """
        if job is not None and job.profile:
            return job.profile
        if repository_id is not None:
            preference = self.store.get_preference(f"profile:{repository_id}")
            if preference:
                return preference
        return self.config.default_profile

    def set_repository_profile(self, repository_id: UUID, profile: str) -> None:
        get_workflow(profile)  # refuse to store a profile that does not exist
        self.store.set_preference(f"profile:{repository_id}", profile)

    async def start_run(self, workflow_name: str, *, job_id: UUID, request: str = "") -> WorkflowRun:
        """Begin a composite workflow over a job and start its first applicable step."""
        definition = get_workflow(workflow_name)
        if not definition.is_composite:
            raise SessionManagerError(f"{definition.name} is not a composite workflow.")
        job = self.store.get_job(job_id)
        if job is None:
            raise SessionManagerError(f"Job {job_id} does not exist.")
        existing = self.store.active_run(job.id)
        if existing is not None:
            raise SessionManagerError(
                f"{job.title!r} is already running {existing.workflow} "
                f"(step {existing.step_index + 1}, {existing.status.value})."
            )
        job.profile = definition.name
        self.store.save_job(job)
        run = WorkflowRun(
            job_id=job.id,
            workflow=definition.name,
            request=request,
            head_at_start=self._job_head(job),
        )
        self.store.save_run(run)
        self.emit(
            ev.RUN_STARTED, job_id=job.id, summary=f"{definition.name} started for {job.title!r}."
        )
        return await self.advance_run(run.id)

    async def advance_run(self, run_id: UUID) -> WorkflowRun:
        """Move a run to its next applicable step, or pause it and say why.

        Called once when a run starts and once each time its current worker finishes a
        turn. Every decision here reads stored state, so a run resumed after a restart
        behaves identically.
        """
        run = self.store.get_run(run_id)
        if run is None:
            raise SessionManagerError(f"Run {run_id} does not exist.")
        if run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            return run
        job = self.store.get_job(run.job_id)
        if job is not None:
            self._reconcile_job_git(job)
        definition = self._definition(run.workflow)
        if job is None or definition is None or not definition.is_composite:
            return self._pause_run(run, RunStatus.FAILED, "Its workflow or job no longer exists.")
        steps = definition.steps

        # The step that just finished decides whether the run may continue.
        if run.current_worker_id is not None:
            if run.step_index < len(steps) and not self._approval_satisfied(run, steps[run.step_index], job):
                return self._pause_run(
                    run,
                    RunStatus.AWAITING_APPROVAL,
                    f"Step {run.step_index + 1} ({steps[run.step_index].workflow}) needs your approval.",
                )
            run.current_worker_id = None
            run.step_index += 1

        while True:
            head = self._job_head(job)
            if run.step_index >= len(steps):
                repeat = self._repeat_target(run, definition, job, head)
                if repeat is None:
                    run.status = RunStatus.COMPLETED
                    run.detail = "Every applicable step is done."
                    run.updated_at = now()
                    self.store.save_run(run)
                    self.emit(ev.RUN_COMPLETED, job_id=job.id, summary=run.detail)
                    return run
                run.step_index = repeat
            step = steps[run.step_index]
            step_definition = self._definition(step.workflow)
            if step_definition is None:
                return self._pause_run(
                    run, RunStatus.FAILED, f"Step {run.step_index + 1} names an unknown workflow."
                )
            used = run.iterations.get(str(run.step_index), 0)
            applies = used < step.max_iterations and condition_holds(
                step.when,
                store=self.store,
                config=self.config,
                job=job,
                run=run,
                definition=step_definition,
                head=head,
            )
            if not applies:
                run.step_index += 1
                continue
            try:
                self._assert_prerequisites(step_definition, job)
            except SessionManagerError as exc:
                return self._pause_run(run, RunStatus.AWAITING_APPROVAL, str(exc))

            run.status = RunStatus.RUNNING
            run.detail = ""
            self.store.save_run(run)
            try:
                worker = await self.start_workflow(
                    step.workflow,
                    job_id=job.id,
                    target_worker_id=self._worker_for_step(step, step_definition, job),
                    request=run.request,
                )
            except SessionManagerError as exc:
                return self._pause_run(run, RunStatus.BLOCKED, str(exc))
            # `start_workflow` adopts the worker into this step; only fill in if it did not.
            run = self.store.get_run(run.id) or run
            if run.current_worker_id is None:
                run.iterations[str(run.step_index)] = used + 1
                run.current_worker_id = worker.id
                run.updated_at = now()
                self.store.save_run(run)
            return run

    def _approval_satisfied(self, run: WorkflowRun, step: WorkflowStep, job: Job) -> bool:
        """Whether the user has given the approval this step asks for."""
        if step.approval is Approval.NONE:
            return True
        blocking = has_blocking_decisions(self.store, job)
        if step.approval is Approval.ONLY_IF_DECISIONS and not blocking:
            return True
        if blocking:
            return False
        step_definition = self._definition(step.workflow)
        if step_definition is not None and (
            ArtifactType.IMPLEMENTATION_CONTRACT in step_definition.produces
        ):
            artifact = self.store.latest_artifact(job.id, ArtifactType.IMPLEMENTATION_CONTRACT)
            if artifact is None:
                return False
            return ImplementationContract.model_validate(artifact.body).approved
        return run.step_index in run.approved_steps

    def _repeat_target(
        self, run: WorkflowRun, definition: WorkflowDefinition, job: Job, head: str | None
    ) -> int | None:
        """The earliest repeatable step whose condition still holds, or None.

        This is the only way a run moves backwards, and `max_iterations` bounds it, so a
        fix/verify/review loop can never spin.
        """
        for index, step in enumerate(definition.steps):
            if step.max_iterations <= 1:
                continue
            if run.iterations.get(str(index), 0) >= step.max_iterations:
                continue
            step_definition = self._definition(step.workflow)
            if step_definition is None:
                continue
            if condition_holds(
                step.when,
                store=self.store,
                config=self.config,
                job=job,
                run=run,
                definition=step_definition,
                head=head,
            ):
                return index
        return None

    def _worker_for_step(
        self, step: WorkflowStep, definition: WorkflowDefinition, job: Job
    ) -> UUID | None:
        """The worker a step should run on; None means start a fresh independent session."""
        mode = step.worker if step.worker is not WorkerMode.AUTO else definition.worker
        if mode is WorkerMode.FRESH:
            return None
        candidates = [
            w
            for w in self.store.list_workers(job.id)
            if w.role in definition.allowed_roles
            and w.status not in (WorkerStatus.STOPPED, WorkerStatus.FAILED, WorkerStatus.DISCONNECTED)
            and (w.writable or not definition.mutates_code)
        ]
        return candidates[-1].id if candidates else None

    def _pause_run(self, run: WorkflowRun, status: RunStatus, detail: str) -> WorkflowRun:
        run.status = status
        run.detail = detail
        run.updated_at = now()
        self.store.save_run(run)
        self.emit(ev.RUN_PAUSED, job_id=run.job_id, summary=f"{run.workflow}: {detail}")
        return run

    async def resume_run(self, run_id: UUID) -> WorkflowRun:
        """Record the user's approval for the paused step and continue the run."""
        run = self.store.get_run(run_id)
        if run is None:
            raise SessionManagerError(f"Run {run_id} does not exist.")
        if run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            raise SessionManagerError(f"That run already {run.status.value}.")
        if run.step_index not in run.approved_steps:
            run.approved_steps = [*run.approved_steps, run.step_index]
        run.status = RunStatus.RUNNING
        self.store.save_run(run)
        return await self.advance_run(run.id)

    def _job_head(self, job: Job) -> str | None:
        head, _ = self._job_head_and_dirty(job)
        return head

    def _pause_run_of(self, worker: Worker, status: RunStatus, detail: str) -> None:
        run = self.store.run_for_worker(worker.id)
        if run is not None and run.status is RunStatus.RUNNING:
            self._pause_run(run, status, detail)

    def _schedule_run_advance(self, worker: Worker) -> None:
        """Continue the run this worker is a step of, once its turn has finished."""
        if worker.job_id is None:
            return
        run = self.store.run_for_worker(worker.id)
        if run is None or run.status is not RunStatus.RUNNING:
            return
        self._spawn(self.advance_run(run.id))

    def _spawn(self, coro) -> None:
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:  # no running loop (a synchronous unit test)
            coro.close()
            return
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _assert_prerequisites(self, definition: WorkflowDefinition, job: Job | None) -> None:
        """A workflow cannot run before the artifacts it declares it needs exist.

        This is what stops implementation from starting without an approved plan, however
        confidently a model asks for it.
        """
        if not definition.required_artifacts:
            return
        if job is None:
            raise SessionManagerError(
                f"{definition.name} needs a job carrying "
                f"{', '.join(sorted(a.value for a in definition.required_artifacts))}."
            )
        for required in sorted(definition.required_artifacts, key=lambda a: a.value):
            artifact = self.store.latest_artifact(job.id, required)
            if artifact is None or artifact.stale:
                raise SessionManagerError(
                    f"{definition.name} needs a current {required.value} for this job and there "
                    "is none. Run plan-feature first, then approve the plan."
                )
            if (
                required is ArtifactType.IMPLEMENTATION_CONTRACT
                and definition.mutates_code
                and self.config.commits.require_plan
            ):
                contract = ImplementationContract.model_validate(artifact.body)
                if not contract.approved:
                    raise SessionManagerError(
                        f"{definition.name} needs an approved implementation contract. "
                        "The plan exists but has not been approved yet."
                    )
                if contract.blocking_decisions():
                    raise SessionManagerError(
                        f"{definition.name} is blocked on "
                        f"{len(contract.blocking_decisions())} unanswered decision(s): "
                        f"{contract.blocking_decisions()[0].question}"
                    )

    def _advance_stage(self, job: Job | None, definition: WorkflowDefinition) -> None:
        """Each workflow declares the stage it moves its job to; unset means no change.

        `ready_to_push` is the exception: it is a claim about the state of the change, so
        it is granted by the deterministic gate rather than by a workflow declaring it.
        """
        if job is None or definition.stage is None:
            return
        if definition.stage is JobStage.READY_TO_PUSH and not self.ready_to_push(job.id).ready:
            return
        self.update_job_stage(job, definition.stage)

    def _note_execution(self, job: Job | None, worker: Worker, workflow_name: str) -> None:
        self.store.add_workflow_execution(
            WorkflowExecution(
                job_id=job.id if job else None,
                worker_id=worker.id,
                workflow=workflow_name,
                head_commit=self._head(worker),
            )
        )

    def _render(self, definition: WorkflowDefinition, job: Job | None, request: str) -> str:
        """Render a workflow's prompt template from stored state.

        Every value here is derived from what the workflow declares, so a user-defined
        workflow gets the same substitutions as a built-in one.
        """
        rebase = self.config.workflows.rebase_stack
        values: dict[str, object] = {
            "request": request or (job.ticket_text if job else "") or (job.title if job else ""),
            "artifacts": self._artifact_block(job, definition),
            "plan_max_lines": self.config.workflows.plan_feature.max_plan_lines,
            "scope": definition.scope,
            "base_ref": job.base_ref if job else "main",
            "preserve_merges": rebase.preserve_merges,
            "autosquash_fixups": rebase.autosquash_fixups,
            "never_force_push": rebase.never_force_push,
            "base_commit": "",
            "head_commit": "",
            "commits": "",
            "diff": "",
        }
        # A workflow that mines rituals is given Switchboard's own history instead of a repository.
        if ArtifactType.WORKFLOW_PROPOSALS in definition.produces:
            values |= {
                "history": self.workflow_history(),
                "available_workflows": ", ".join(workflow_names()),
            }
        # A workflow that produces a review needs the commit range it is reviewing.
        if job is not None and ArtifactType.REVIEW in definition.produces:
            base, head, commits, diff = self._review_inputs(job)
            values |= {
                "base_commit": base, "head_commit": head, "commits": commits, "diff": diff
            }
        return render_template(definition.template, values)

    def _artifact_block(self, job: Job | None, definition: WorkflowDefinition) -> str:
        """Only the structured artifacts this action declares -- never a planner transcript."""
        if job is None:
            return "(none)"
        wanted = definition.prompt_context
        lines: list[str] = []
        for type_ in sorted(wanted, key=lambda t: t.value):
            artifact = self.store.latest_artifact(job.id, type_)
            if artifact and not artifact.stale:
                lines.append(f"### {type_.value}\n{artifact.body}")
        for decision in self.store.list_decisions(job.id):
            lines.append(f"### decision\nQ: {decision.question}\nA: {decision.answer}")
        return "\n\n".join(lines) if lines else "(none)"

    def _review_inputs(self, job: Job) -> tuple[str, str, str, str]:
        path = self._job_inspection_path(job)
        if path is None:
            return "", "", "(no worktree)", "(no diff available)"
        try:
            base = runner.run_git(path, "merge-base", job.base_ref, "HEAD").out
            head = runner.head_commit(path)
            commits = "\n".join(runner.commits_between(path, base, head)) or "(no commits yet)"
            return base, head, commits, runner.diff(path, base, head) or "(empty diff)"
        except GitError as exc:
            return "", "", f"(git error: {exc})", "(no diff available)"

    # ---------------------------------------------------------------- mining

    def workflow_history(self, limit: int = 200) -> str:
        """Switchboard's own record of what was run, grouped by job and ordered in time.

        This is the whole input to mining. It is structured state rather than any
        transcript, so it stays small and contains no repository content -- but it does
        span every registered repository, so a miner running in one worktree sees the
        job titles and decisions of the others.
        """
        executions = self.store.recent_workflow_executions(limit)
        if not executions:
            return "(no workflow history yet)"
        titles = {job.id: (job.external_ref or job.title) for job in self.store.list_jobs()}
        by_job: dict[UUID | None, list[WorkflowExecution]] = {}
        for execution in executions:
            by_job.setdefault(execution.job_id, []).append(execution)
        lines: list[str] = []
        for job_id, runs in by_job.items():
            lines.append(f"### {titles.get(job_id, 'ad-hoc work') if job_id else 'ad-hoc work'}")
            for run in runs:
                when = run.created_at.strftime("%Y-%m-%d %H:%M")
                head = f" at {run.head_commit[:8]}" if run.head_commit else ""
                lines.append(f"- {when}  {run.workflow} ({run.status}){head}")
            if job_id is not None:
                for decision in self.store.list_decisions(job_id):
                    lines.append(f"  decision: {decision.question} -> {decision.answer}")
            lines.append("")
        return "\n".join(lines).strip()

    def list_proposals(self, job_id: UUID) -> list[WorkflowProposal]:
        """The proposals from this job's most recent mining run, if any."""
        artifact = self.store.latest_artifact(job_id, ArtifactType.WORKFLOW_PROPOSALS)
        if artifact is None:
            return []
        return WorkflowProposals.model_validate(artifact.body).proposals

    def accept_proposal(self, job_id: UUID, name: str) -> Path:
        """Turn one accepted proposal into an ordinary user workflow file.

        The written file is the same YAML a user would have hand-authored, in the same
        directory, with no marker distinguishing it -- an accepted proposal is a workflow,
        not a second-class kind of one. Only this method makes a proposal take effect.

        Everything a model chose is validated here before anything is written. A proposal
        comes from free text: its step conditions, its worker mode, and its name are all
        fields a model can paraphrase. Writing first and discovering at load time that the
        file is invalid would tell the user their workflow exists when it does not.
        """
        proposal = next((p for p in self.list_proposals(job_id) if p.name == name), None)
        if proposal is None:
            raise SessionManagerError(f"No proposal named {name!r} on this job.")
        if not proposal.steps:
            raise SessionManagerError(f"Proposal {name!r} has no steps.")
        unknown = [s.workflow for s in proposal.steps if s.workflow not in workflow_names()]
        if unknown:
            raise SessionManagerError(
                f"Proposal {name!r} names workflows that do not exist: {', '.join(unknown)}."
            )
        composite = [s.workflow for s in proposal.steps if get_workflow(s.workflow).is_composite]
        if composite:
            raise SessionManagerError(
                f"Proposal {name!r} uses composite workflows as steps, which cannot nest: "
                f"{', '.join(composite)}."
            )
        if proposal.name in builtin_names():
            raise SessionManagerError(
                f"{proposal.name!r} is a built-in workflow and cannot be redefined."
            )
        document = {
            "name": proposal.name,
            "description": proposal.description,
            "worker": proposal.worker,
            "steps": [{"workflow": s.workflow, "when": s.when} for s in proposal.steps],
        }
        try:
            WorkflowDefinition.model_validate(document)
        except ValidationError as exc:
            raise SessionManagerError(
                f"Proposal {name!r} is not a valid workflow: {exc.errors()[0]['msg']}"
            ) from exc

        directory = user_workflows_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{slug(proposal.name)}.yaml"
        if path.exists():
            raise SessionManagerError(f"{path} already exists; edit or remove it first.")
        path.write_text(yaml.safe_dump(document, sort_keys=False))
        problems = self.reload_workflows()
        if proposal.name not in workflow_names():  # pragma: no cover - validated above
            path.unlink(missing_ok=True)
            raise SessionManagerError(
                f"Proposal {name!r} did not load as a workflow: {'; '.join(problems)}"
            )
        self.emit(
            ev.WORKFLOW_PROPOSAL_ACCEPTED,
            job_id=job_id,
            summary=f"Accepted workflow proposal {proposal.name}.",
            payload={"path": str(path)},
        )
        return path

    # ------------------------------------------------------------ interruption

    async def interrupt_worker(self, worker_id: UUID) -> None:
        """Stop the current turn. The worker stays alive and can be messaged again.

        The interruption is recorded in the transcript rather than in `waiting_for`,
        because the backend's own end-of-turn event legitimately clears that field a
        moment later. Any attention the worker was holding is resolved, so auto-advance
        does not immediately bounce back to the worker the user just interrupted.
        """
        worker = self._require_worker(worker_id)
        await self.backend.interrupt(worker_id)
        self._record(worker, "system", "[interrupted by the user]")
        self._resolve_attention(worker)
        self._set_status(worker, WorkerStatus.IDLE, waiting_for=None)
        self._set_runtime_state(worker.id, RuntimeProcessState.READY)

    async def stop_worker(self, worker_id: UUID) -> None:
        worker = self._require_worker(worker_id)
        pump = self._pumps.pop(worker_id, None)
        await self.backend.stop(worker_id)
        if pump is not None:
            pump.cancel()
        self._set_status(worker, WorkerStatus.STOPPED, waiting_for=None)
        self._set_runtime_state(worker.id, RuntimeProcessState.EXITED)
        self.emit(ev.WORKER_STOPPED, worker_id=worker.id, job_id=worker.job_id, summary=worker.title)

    # ------------------------------------------------------------------ attach

    async def attach(self, worker_id: UUID) -> Attachment:
        """Hand this worker's session back to the user as an ordinary Claude session.

        A worker mid-turn is interrupted first, and until `detach` it is marked attached:
        `send` refuses, so nothing Switchboard does appends to a session file the user's own
        client is writing. Any composite run the worker belongs to pauses in a resumable
        state -- what happens next is the user's to decide, not the run's, but deciding
        "carry on" has to remain possible.
        """
        worker = self._require_worker(worker_id)
        attachment = build_attachment(
            cwd=worker.cwd,
            session_id=worker.session_id,
            executable=self.config.claude.executable,
            note=self._attach_note(worker),
        )
        if worker.status is WorkerStatus.WORKING:
            await self.interrupt_worker(worker_id)
        runtime = self.store.current_runtime(worker.id)
        if runtime is None:
            raise AttachError("This worker has no durable runtime instance.")
        self._snapshot_before_change(worker)
        runtime = self.store.current_runtime(worker.id) or runtime
        runtime.owner = RuntimeOwner.HUMAN
        runtime.updated_at = now()
        self.store.save_runtime(runtime)
        self._pause_run_of(worker, RunStatus.BLOCKED, "The user attached to this worker.")
        self._record(worker, "system", "[the user attached to this session directly]")
        self._resolve_attention(worker)
        self.emit(
            ev.WORKER_ATTACHED,
            worker_id=worker.id,
            job_id=worker.job_id,
            summary=f"Attached to {worker.title}.",
            payload={"session_id": attachment.session_id, "cwd": str(attachment.cwd)},
        )
        return attachment

    def detach(self, worker_id: UUID) -> Worker:
        """The user has left the session. Switchboard may drive it again; the run stays paused.

        The run is deliberately not resumed here. The user has just been editing in that
        worktree by hand, so whether the ritual should carry on from where it stopped is
        a judgement only they can make -- `resume_run` is how they say yes.
        """
        worker = self._require_worker(worker_id)
        runtime = self.store.current_runtime(worker.id)
        if runtime is not None:
            runtime.owner = RuntimeOwner.MANAGER
            runtime.updated_at = now()
            self.store.save_runtime(runtime)
        self._apply_invalidation(
            worker,
            self.store.get_job(worker.job_id) if worker.job_id else None,
            force=True,
        )
        self._record(worker, "system", "[the user left this session]")
        return worker

    def is_attached(self, worker_id: UUID) -> bool:
        runtime = self.store.current_runtime(worker_id)
        return runtime is not None and runtime.owner is RuntimeOwner.HUMAN

    def _attach_note(self, worker: Worker) -> str:
        """What the user should know before taking this session over, if anything.

        Attaching starts an ordinary interactive Claude, which has none of the tool
        restrictions Switchboard gave the worker. That is the user's prerogative -- but a
        read-only worker usually sits in *another* worker's worktree, so it is worth
        saying that the session they are about to drive can write there.
        """
        if worker.writable or worker.worktree_id is not None:
            return ""
        owner = next(
            (
                other
                for other in self.store.list_workers(worker.job_id)
                if other.writable and other.cwd == worker.cwd and other.id != worker.id
            ),
            None,
        )
        if owner is None:
            return (
                f"This worker is read-only, but {worker.cwd} is the repository itself. "
                "An interactive session there is not restricted."
            )
        return (
            f"This worker is read-only and observes {owner.title}'s worktree. An "
            "interactive session there can write to it, and that worker still owns it."
        )

    # ----------------------------------------------------------------- cleanup

    async def request_cleanup(
        self, *, worker_id: UUID | None = None, job_id: UUID | None = None, confirmed: bool = False
    ) -> CleanupResult:
        """Stop a worker and remove only state that is provably safe to remove."""
        if not confirmed:
            decision = CleanupDecision(
                safe=False, reasons=["Cleanup is destructive and needs explicit confirmation."]
            )
            self.emit(ev.CLEANUP_REFUSED, worker_id=worker_id, job_id=job_id, summary=decision.explanation)
            return CleanupResult(performed=False, decision=decision)

        workers = (
            [self._require_worker(worker_id)]
            if worker_id
            else self.store.list_workers(job_id)
            if job_id
            else []
        )
        if not workers:
            raise SessionManagerError("Cleanup needs a worker or a job with workers.")

        reasons: list[str] = []
        cleaned = 0
        for worker in workers:
            if worker.worktree_id:
                worktree = self.store.get_worktree(worker.worktree_id)
                repo = self.store.get_repository(worker.repository_id)
                if worktree and repo:
                    decision = self.worktrees.can_cleanup(worktree)
                    if not decision.safe:
                        reasons.extend(decision.reasons)
                        self.emit(
                            ev.CLEANUP_REFUSED,
                            worker_id=worker.id,
                            job_id=worker.job_id,
                            summary=decision.explanation,
                        )
                        continue
                    self.worktrees.cleanup_worktree(repo, worktree)
                    self.store.delete_worktree(worktree.id)
            if worker.status != WorkerStatus.STOPPED:
                await self.stop_worker(worker.id)
            cleaned += 1
            self.emit(
                ev.CLEANUP_COMPLETED,
                worker_id=worker.id,
                job_id=worker.job_id,
                summary=f"Cleaned up {worker.title!r}.",
            )
        if reasons:
            return CleanupResult(performed=cleaned > 0, decision=CleanupDecision(False, reasons))
        return CleanupResult(
            performed=True,
            decision=CleanupDecision(True, [f"Cleaned up {cleaned} worker(s); branches preserved."]),
        )

    # --------------------------------------------------------------- attention

    def list_attention_items(self) -> list[AttentionItem]:
        from switchboard.routing.attention import prioritize

        workers = {w.id: w for w in self.store.list_workers()}
        return prioritize(self.store.list_attention_items(), workers)

    def raise_attention(
        self, worker: Worker, kind: AttentionKind, reason: str, waiting_for: str | None = None
    ) -> AttentionItem:
        item = AttentionItem(
            worker_id=worker.id,
            job_id=worker.job_id,
            kind=kind,
            reason=reason[:300],
            waiting_for=waiting_for,
        )
        return self.store.save_attention_item(item)

    def _resolve_attention(self, worker: Worker) -> None:
        for item in self.store.attention_items_for_worker(worker.id):
            item.handled = True
            self.store.save_attention_item(item)

    def record_decision(self, job_id: UUID, question: str, answer: str) -> Decision:
        job = self.store.get_job(job_id)
        if job is None:
            raise SessionManagerError(f"Job {job_id} does not exist.")
        decision = self.store.add_decision(Decision(job_id=job_id, question=question, answer=answer))
        contract = self.store.latest_artifact(job_id, ArtifactType.IMPLEMENTATION_CONTRACT)
        if contract is not None:
            parsed = ImplementationContract.model_validate(contract.body)
            for pending in parsed.decisions:
                if pending.question.strip().lower() == question.strip().lower():
                    pending.blocking = False
            contract.body = parsed.model_dump(mode="json")
            self.store.save_artifact(contract)
        return decision

    def approve_plan(self, job_id: UUID) -> Artifact:
        contract = self.store.latest_artifact(job_id, ArtifactType.IMPLEMENTATION_CONTRACT)
        if contract is None:
            raise SessionManagerError("There is no implementation contract to approve.")
        parsed = ImplementationContract.model_validate(contract.body)
        blocking = [d for d in parsed.blocking_decisions()]
        if blocking:
            raise SessionManagerError(
                f"{len(blocking)} blocking decision(s) are unanswered: {blocking[0].question}"
            )
        parsed.approved = True
        contract.body = parsed.model_dump(mode="json")
        self.store.save_artifact(contract)
        # The plan no longer needs the user, so it leaves the attention queue.
        for item in self.store.list_attention_items():
            if item.job_id == job_id and item.kind is AttentionKind.PLAN_APPROVAL:
                item.handled = True
                self.store.save_attention_item(item)
        self.emit(ev.PLAN_APPROVED, job_id=job_id, summary="Plan approved.")
        # Approval is what an approval-gated run was waiting for, so it continues here.
        run = self.store.active_run(job_id)
        if run is not None and run.status is RunStatus.AWAITING_APPROVAL:
            self._spawn(self.resume_run(run.id))
        return contract

    # ------------------------------------------------------------- pin/snooze

    def toggle_pin(self, worker_id: UUID) -> Worker:
        worker = self._require_worker(worker_id)
        worker.pinned = not worker.pinned
        return self.store.save_worker(worker)

    def snooze(self, worker_id: UUID, minutes: int = 30) -> Worker:
        from datetime import timedelta

        worker = self._require_worker(worker_id)
        worker.snoozed_until = now() + timedelta(minutes=minutes)
        return self.store.save_worker(worker)

    # ------------------------------------------------------------ event pump

    async def _pump(self, worker_id: UUID) -> None:
        try:
            async for event in self.backend.stream(worker_id):
                try:
                    self._apply(event)
                except Exception:
                    log.exception("failed to apply worker event %s", event.type)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("event pump for %s stopped", worker_id)

    def _apply(self, event: WorkerEvent) -> None:
        worker = self.store.get_worker(event.worker_id)
        if worker is None:
            return
        match event.type:
            case "session":
                worker.session_id = event.text
                self.store.save_worker(worker)
                runtime = self.store.current_runtime(worker.id)
                if runtime is not None:
                    runtime.claude_session_id = event.text
                    runtime.updated_at = now()
                    self.store.save_runtime(runtime)
            case "text":
                self._record(worker, "assistant", event.text)
                self.emit(ev.WORKER_OUTPUT, worker_id=worker.id, job_id=worker.job_id)
            case "tool":
                self._record(worker, "tool", f"[{event.text}]")
            case "helper":
                worker.active_helpers = int(event.data.get("active", 0))
                self.store.save_worker(worker)
            case "permission":
                self._set_runtime_state(worker.id, RuntimeProcessState.WAITING)
                self._set_status(worker, WorkerStatus.BLOCKED, waiting_for=event.text)
                self.raise_attention(
                    worker, AttentionKind.PERMISSION_REQUIRED, event.text, event.text
                )
                self.emit(ev.WORKER_PERMISSION_REQUIRED, worker_id=worker.id, job_id=worker.job_id)
            case "blocked":
                self._set_runtime_state(worker.id, RuntimeProcessState.WAITING)
                self._finish_turn(worker, event.text)
                reason = _last_question(event.text)
                self._set_status(worker, WorkerStatus.BLOCKED, waiting_for=reason)
                kind = (
                    AttentionKind.PLAN_APPROVAL
                    if worker.role == WorkerRole.PLANNER
                    else AttentionKind.HUMAN_DECISION
                )
                self.raise_attention(worker, kind, reason, reason)
                self.emit(
                    ev.WORKER_BLOCKED, worker_id=worker.id, job_id=worker.job_id, summary=reason
                )
                # A step that stopped to ask the user pauses its run rather than being
                # treated as finished; the answer resumes it.
                self._pause_run_of(worker, RunStatus.AWAITING_APPROVAL, reason)
            case "result":
                self._finish_turn(worker, event.text)
                self._set_runtime_state(worker.id, RuntimeProcessState.TURN_COMPLETE)
                if event.data.get("is_error"):
                    self._set_status(worker, WorkerStatus.FAILED, waiting_for="Turn failed.")
                    self.raise_attention(
                        worker, AttentionKind.WORKER_FAILED, "The worker's turn failed."
                    )
                    self.emit(ev.WORKER_FAILED, worker_id=worker.id, job_id=worker.job_id)
                    self._pause_run_of(worker, RunStatus.BLOCKED, "The step's worker failed.")
                else:
                    self._set_status(worker, WorkerStatus.IDLE, waiting_for=None)
                    self.emit(ev.WORKER_COMPLETED, worker_id=worker.id, job_id=worker.job_id)
                    self._schedule_run_advance(worker)
            case "failed":
                self._set_runtime_state(worker.id, RuntimeProcessState.EXITED)
                self._set_status(worker, WorkerStatus.FAILED, waiting_for=event.text[:200])
                self.raise_attention(worker, AttentionKind.WORKER_FAILED, event.text[:200])
                self.emit(
                    ev.WORKER_FAILED, worker_id=worker.id, job_id=worker.job_id, summary=event.text
                )
                self._pause_run_of(worker, RunStatus.BLOCKED, event.text[:200])
            case "stopped":
                self._set_runtime_state(worker.id, RuntimeProcessState.EXITED)
                self._set_status(worker, WorkerStatus.STOPPED, waiting_for=None)

    def _finish_turn(self, worker: Worker, text: str) -> None:
        """Harvest artifacts and apply Git-derived invalidation at the end of a turn."""
        job = self.store.get_job(worker.job_id) if worker.job_id else None
        if job is not None:
            self._harvest_artifact(worker, job, text)
        self._apply_invalidation(worker, job)

    # --------------------------------------------------------------- artifacts

    def _harvest_artifact(self, worker: Worker, job: Job, text: str) -> None:
        """Turn a worker's fenced JSON block into the artifact its workflow declares.

        Dispatch is on what the workflow says it *produces*, not on its name, so a
        user-defined workflow that produces a verification is harvested like any other.
        """
        block = extract_json_block(text)
        if block is None:
            return
        head = self._head(worker)
        tree = self._tree(worker)
        produces = self._produced_artifacts(worker)

        if ArtifactType.IMPLEMENTATION_CONTRACT in produces:
            self._store_plan(worker, job, block, head, tree)
        elif produces & {ArtifactType.VERIFICATION, ArtifactType.SMOKE_VERIFICATION}:
            type_ = (
                ArtifactType.SMOKE_VERIFICATION
                if ArtifactType.SMOKE_VERIFICATION in produces
                else ArtifactType.VERIFICATION
            )
            self._store_verification(worker, job, block, head, tree, type_)
        elif ArtifactType.REVIEW in produces:
            self._store_review(worker, job, block, head, tree)
        elif ArtifactType.COMMENT_RESOLUTIONS in produces:
            report = CommentResolutionReport.model_validate(block)
            self._save_artifact(
                job, ArtifactType.COMMENT_RESOLUTIONS, worker, report.model_dump(mode="json"), head, tree
            )
        elif ArtifactType.WORKFLOW_PROPOSALS in produces:
            proposals = WorkflowProposals.model_validate(block)
            self._save_artifact(
                job, ArtifactType.WORKFLOW_PROPOSALS, worker, proposals.model_dump(mode="json"), head, tree
            )
            if proposals.proposals:
                names = ", ".join(p.name for p in proposals.proposals)
                self.raise_attention(
                    worker,
                    AttentionKind.HUMAN_DECISION,
                    f"Proposed workflows awaiting your decision: {names}.",
                    "Accept, edit, or reject each proposal.",
                )

    def _produced_artifacts(self, worker: Worker) -> frozenset[ArtifactType]:
        """What this worker's turn may produce: its workflow's declaration, else its role."""
        definition = self._definition(worker.workflow)
        if definition is not None and definition.produces:
            return definition.produces
        return ROLE_ARTIFACTS.get(worker.role, frozenset())

    def _definition(self, workflow: str | None) -> WorkflowDefinition | None:
        if not workflow:
            return None
        try:
            return get_workflow(workflow)
        except WorkflowError:
            return None

    def _store_plan(self, worker: Worker, job: Job, block: dict, head: str | None, tree: str | None) -> None:
        contract = ImplementationContract.model_validate(
            {k: v for k, v in block.items() if k != "criteria"}
        )
        if not contract.base_commit:
            contract.base_commit = head or ""
        max_lines = self.config.workflows.plan_feature.max_plan_lines
        contract.summary_lines = contract.summary_lines[:max_lines]
        self._save_artifact(
            job, ArtifactType.IMPLEMENTATION_CONTRACT, worker, contract.model_dump(mode="json"), head, tree
        )
        behavior = BehaviorContract.model_validate({"criteria": block.get("criteria", [])})
        self._save_artifact(
            job, ArtifactType.BEHAVIOR_CONTRACT, worker, behavior.model_dump(mode="json"), head, tree
        )
        self.emit(ev.PLAN_CREATED, job_id=job.id, worker_id=worker.id, summary="Contracts recorded.")
        if contract.blocking_decisions():
            self.emit(ev.PLAN_REQUIRES_INPUT, job_id=job.id, worker_id=worker.id)

    def _store_verification(
        self,
        worker: Worker,
        job: Job,
        block: dict,
        head: str | None,
        tree: str | None,
        type_: ArtifactType,
    ) -> None:
        report = VerificationReport.model_validate(block)
        report.tested_head = head or ""
        for evidence in report.evidence:
            evidence.tested_head = head or ""
        self._save_artifact(job, type_, worker, report.model_dump(mode="json"), head, tree)
        self._sync_criteria_status(job, report)
        if report.passed:
            self.emit(ev.VERIFICATION_PASSED, job_id=job.id, worker_id=worker.id)
        else:
            failed = [e.criterion_id for e in report.evidence if e.status != "passed"]
            self.emit(
                ev.VERIFICATION_FAILED,
                job_id=job.id,
                worker_id=worker.id,
                summary=f"Failed: {', '.join(failed)}",
            )
            self.raise_attention(
                worker,
                AttentionKind.VERIFICATION_FAILED,
                f"Verification failed for {', '.join(failed)}.",
            )

    def _store_review(self, worker: Worker, job: Job, block: dict, head: str | None, tree: str | None) -> None:
        report = ReviewReport.model_validate(block)
        report.reviewed_head = head or ""
        for finding in report.findings:
            finding.reviewed_head = head or ""
        self._save_artifact(job, ArtifactType.REVIEW, worker, report.model_dump(mode="json"), head, tree)
        blocking = report.unresolved_blocking(set(self.config.workflows.review_change.blocking_severities))
        if blocking:
            self.emit(
                ev.REVIEW_BLOCKING_FINDINGS,
                job_id=job.id,
                worker_id=worker.id,
                summary=f"{len(blocking)} blocking finding(s).",
            )
            self.raise_attention(
                worker,
                AttentionKind.BLOCKING_REVIEW_FINDING,
                f"{len(blocking)} blocking finding(s): {blocking[0].description[:120]}",
            )
            self.update_job_stage(job, JobStage.FIXING)
        else:
            self.emit(ev.REVIEW_PASSED, job_id=job.id, worker_id=worker.id)
            self._maybe_ready_to_push(job, worker)

    def _sync_criteria_status(self, job: Job, report: VerificationReport) -> None:
        artifact = self.store.latest_artifact(job.id, ArtifactType.BEHAVIOR_CONTRACT)
        if artifact is None:
            return
        behavior = BehaviorContract.model_validate(artifact.body)
        by_id = {e.criterion_id: e for e in report.evidence}
        for criterion in behavior.criteria:
            evidence = by_id.get(criterion.id)
            if evidence is None:
                continue
            criterion.status = "passed" if evidence.status == "passed" else (
                "blocked" if evidence.status in ("blocked", "not_tested") else "failed"
            )
            if evidence.limitations:
                criterion.accepted_limitation = "; ".join(evidence.limitations)
        artifact.body = behavior.model_dump(mode="json")
        self.store.save_artifact(artifact)

    def _save_artifact(
        self,
        job: Job,
        type_: ArtifactType,
        worker: Worker,
        body: dict,
        head: str | None,
        tree: str | None,
    ) -> Artifact:
        artifact = Artifact(
            job_id=job.id,
            type=type_,
            worker_id=worker.id,
            base_commit=job.base_ref,
            head_commit=head,
            tree_hash=tree,
            body=body,
        )
        return self.store.save_artifact(artifact)

    # ------------------------------------------------------------ invalidation

    def _snapshot_before_change(self, worker: Worker) -> None:
        # Any turn a writable worker takes can change the tree, whatever workflow it is
        # running, so the snapshot is taken from writability rather than from intent.
        if not worker.writable:
            return
        head, tree = self._head(worker), self._tree(worker)
        if head and tree:
            runtime = self.store.current_runtime(worker.id)
            if runtime is not None:
                runtime.git_head_before_turn = head
                runtime.git_tree_before_turn = tree
                runtime.updated_at = now()
                self.store.save_runtime(runtime)

    def _apply_invalidation(
        self, worker: Worker, job: Job | None, *, force: bool = False
    ) -> None:
        runtime = self.store.current_runtime(worker.id)
        if (
            runtime is None
            or runtime.git_head_before_turn is None
            or runtime.git_tree_before_turn is None
        ):
            return
        if runtime.owner is RuntimeOwner.HUMAN and not force:
            # An interrupt completion may arrive after ownership was handed over. Keep
            # the baseline until detach/recovery observes the human's complete edit.
            return
        before = GitSnapshot(runtime.git_head_before_turn, runtime.git_tree_before_turn)
        runtime.git_head_before_turn = None
        runtime.git_tree_before_turn = None
        runtime.updated_at = now()
        self.store.save_runtime(runtime)
        if job is None:
            return
        definition = self._definition(worker.workflow)
        head, tree = self._head(worker), self._tree(worker)
        if not head or not tree:
            return
        change = classify_change(before, GitSnapshot(head, tree))
        if change is CodeChange.NONE:
            return
        if not artifacts_invalidated_by(change):
            # Same tree: behavioral evidence still holds, only lineage moves forward.
            for artifact in self.store.list_artifacts(job.id):
                if artifact.type in BEHAVIORAL_ARTIFACTS and not artifact.stale:
                    self.store.save_artifact(relineage(artifact, head, tree))
            return
        targets = artifacts_invalidated_by(change)
        if definition is not None:
            targets |= definition.invalidates
        invalidated = 0
        for artifact in self.store.list_artifacts(job.id):
            if artifact.type in targets and not artifact.stale:
                artifact.stale = True
                artifact.stale_reason = f"{change.value} at {head[:8]}"
                self.store.save_artifact(artifact)
                invalidated += 1
        if invalidated:
            self.emit(
                ev.ARTIFACT_INVALIDATED,
                job_id=job.id,
                worker_id=worker.id,
                summary=f"{invalidated} artifact(s) invalidated by {change.value}.",
            )

    def _reconcile_job_git(self, job: Job) -> None:
        """Apply any durable, unfinished Git baselines before trusting run state."""
        for worker in self.store.list_workers(job.id):
            if worker.writable:
                self._apply_invalidation(worker, job)

    # ------------------------------------------------------------ ready to push

    def _maybe_ready_to_push(self, job: Job, worker: Worker) -> None:
        report = self.ready_to_push(job.id)
        if report.ready:
            self.update_job_stage(job, JobStage.READY_TO_PUSH)
            self.raise_attention(
                worker, AttentionKind.READY_TO_PUSH, f"{job.title} is ready to push."
            )
            self.emit(ev.JOB_READY_TO_PUSH, job_id=job.id, worker_id=worker.id, summary=report.blurb)

    def ready_to_push(self, job_id: UUID) -> ReadyToPushReport:
        """Deterministic gate. Every blocker is computed from stored state, not judgment."""
        job = self.store.get_job(job_id)
        if job is None:
            raise SessionManagerError(f"Job {job_id} does not exist.")
        blockers: list[str] = []

        contract_artifact = self.store.latest_artifact(job_id, ArtifactType.IMPLEMENTATION_CONTRACT)
        if contract_artifact is None:
            blockers.append("No implementation contract.")
        else:
            contract = ImplementationContract.model_validate(contract_artifact.body)
            if not contract.approved:
                blockers.append("The implementation contract has not been approved.")
            if contract.blocking_decisions():
                blockers.append(
                    f"{len(contract.blocking_decisions())} blocking decision(s) unanswered."
                )

        behavior_artifact = self.store.latest_artifact(job_id, ArtifactType.BEHAVIOR_CONTRACT)
        criteria = (
            BehaviorContract.model_validate(behavior_artifact.body).criteria
            if behavior_artifact
            else []
        )
        if not criteria:
            blockers.append("No acceptance criteria recorded.")
        for criterion in criteria:
            if criterion.status != "passed" and not criterion.accepted_limitation:
                blockers.append(f"Criterion {criterion.id} is {criterion.status}.")

        head, dirty = self._job_head_and_dirty(job)
        verification = self.store.latest_artifact(job_id, ArtifactType.VERIFICATION)
        if verification is None:
            blockers.append("No verification evidence.")
        elif verification.stale or (head and not is_fresh(verification, head)):
            blockers.append("Verification does not apply to current HEAD.")

        review = self.store.latest_artifact(job_id, ArtifactType.REVIEW)
        if review is None:
            blockers.append("No independent review.")
        elif review.stale or (head and not is_fresh(review, head)):
            blockers.append("Review does not apply to current HEAD.")
        else:
            parsed = ReviewReport.model_validate(review.body)
            unresolved = parsed.unresolved_blocking(
                set(self.config.workflows.review_change.blocking_severities)
            )
            if unresolved:
                blockers.append(f"{len(unresolved)} unresolved blocking review finding(s).")

        if dirty:
            blockers.append(f"The worktree has {len(dirty)} uncommitted change(s).")

        return ReadyToPushReport(
            ready=not blockers, blockers=blockers, blurb=self.verification_blurb(job_id)
        )

    def _job_head_and_dirty(self, job: Job) -> tuple[str | None, list[str]]:
        path = self._job_inspection_path(job)
        if path is None:
            return None, []
        try:
            return runner.head_commit(path), runner.dirty_files(path)
        except GitError:
            return None, []

    def verification_blurb(self, job_id: UUID) -> str:
        """A copy-pastable blurb built only from stored evidence -- never from memory."""
        verification = self.store.latest_artifact(job_id, ArtifactType.VERIFICATION)
        review = self.store.latest_artifact(job_id, ArtifactType.REVIEW)
        lines = ["Verification performed:"]
        limitations: list[str] = []
        if verification is None:
            lines.append("- None recorded.")
        else:
            report = VerificationReport.model_validate(verification.body)
            for evidence in report.evidence:
                commands = ", ".join(
                    f"`{c.command}` (exit {c.exit_code})" for c in evidence.commands
                )
                lines.append(
                    f"- {evidence.criterion_id}: {evidence.status} — {evidence.observed_behavior}"
                    + (f" [{commands}]" if commands else "")
                )
                limitations.extend(evidence.limitations)
            if report.tested_head:
                lines.append(f"- Tested head: {report.tested_head[:12]}")
        if review is not None:
            parsed = ReviewReport.model_validate(review.body)
            open_findings = [f for f in parsed.findings if not f.resolved]
            lines.append(
                f"- Independent review of {parsed.reviewed_head[:12]}: {parsed.verdict}, "
                f"{len(open_findings)} open finding(s)."
            )
        lines.append("")
        lines.append("Limitations:")
        lines.extend(f"- {item}" for item in (limitations or ["None recorded."]))
        return "\n".join(lines)

    # ---------------------------------------------------------------- recovery

    async def recover(self) -> list[str]:
        """Adopt matching live runtimes, reconstruct absent ones, and reject stale ones."""
        notes: list[str] = []
        for worker in self.store.list_workers():
            if worker.status in (WorkerStatus.STOPPED, WorkerStatus.DONE):
                continue
            if worker.worktree_id:
                worktree = self.store.get_worktree(worker.worktree_id)
                if worktree is None or not worktree.path.exists():
                    self._force_status(
                        worker,
                        WorkerStatus.DISCONNECTED,
                        "Its worktree is missing. Create a replacement worker from the stored "
                        "contracts, or clean this one up.",
                    )
                    notes.append(f"{worker.title}: worktree missing")
                    continue
            job = self.store.get_job(worker.job_id) if worker.job_id else None
            self._apply_invalidation(worker, job, force=True)
            if not worker.session_id:
                self._force_status(
                    worker,
                    WorkerStatus.DISCONNECTED,
                    "No session id was captured, so this session cannot be resumed. Start a "
                    "replacement seeded from the stored job artifacts.",
                )
                notes.append(f"{worker.title}: no session id")
                continue
            runtime = self.store.current_runtime(worker.id)
            if runtime is None:
                # Databases created before runtime instances existed are reconstructable
                # from the worker's durable Claude session identity.
                runtime = self.store.save_runtime(self._new_runtime(worker, generation=1))
            try:
                observed = await self.backend.observe(worker.id)
                if observed.exists:
                    if (
                        observed.runtime_id != runtime.id
                        or observed.generation != runtime.generation
                    ):
                        runtime.process_state = RuntimeProcessState.WAITING
                        runtime.updated_at = now()
                        self.store.save_runtime(runtime)
                        self._force_status(
                            worker,
                            WorkerStatus.DISCONNECTED,
                            "A live runtime exists for this worker but its identity or generation "
                            "does not match durable state. Refusing to adopt or replace it.",
                        )
                        notes.append(f"{worker.title}: stale runtime rejected")
                        continue
                    await self._start_backend(worker, prompt="", adopt=True)
                    action = "adopted"
                else:
                    if runtime.owner is RuntimeOwner.HUMAN:
                        runtime.process_state = RuntimeProcessState.ABSENT
                        runtime.updated_at = now()
                        self.store.save_runtime(runtime)
                        self._force_status(
                            worker,
                            WorkerStatus.DISCONNECTED,
                            "This runtime was human-controlled when Switchboard stopped, and "
                            "the backend cannot observe it now. Refusing to recreate it until "
                            "ownership is explicitly returned.",
                        )
                        notes.append(f"{worker.title}: human-owned runtime not observable")
                        continue
                    runtime.process_state = RuntimeProcessState.ABSENT
                    runtime.updated_at = now()
                    self.store.save_runtime(runtime)
                    replacement = self._new_runtime(
                        worker, generation=runtime.generation + 1
                    )
                    self.store.save_runtime(replacement)
                    await self._start_backend(worker, prompt="", resume=True)
                    action = "recreated"
                self._force_status(worker, WorkerStatus.IDLE, None)
                notes.append(f"{worker.title}: {action}")
            except Exception as exc:
                self._force_status(
                    worker,
                    WorkerStatus.DISCONNECTED,
                    f"Could not resume this session: {exc}. Start a replacement seeded from the "
                    "stored job artifacts.",
                )
                notes.append(f"{worker.title}: {exc}")
        return notes

    # ------------------------------------------------------------------ router

    def routing_state(self, confirmed: bool = False) -> RoutingState:
        selected = self.store.get_worker(self.selected_worker_id) if self.selected_worker_id else None
        return RoutingState(
            repositories=self.store.list_repositories(),
            jobs=self.store.list_jobs(),
            workers=self.store.list_workers(),
            selected_worker_id=self.selected_worker_id,
            selected_job_id=selected.job_id if selected else None,
            confirmed=confirmed,
        )

    async def execute_route(self, proposal: RouteProposal, confirmed: bool = False) -> str:
        """Validate and perform a route. Returns the manager's user-visible reply."""
        state = self.routing_state(confirmed=confirmed)
        try:
            proposal = router.validate(proposal, state)
        except RouteError as exc:
            return f"Refused: {exc}"

        match proposal.action:
            case "confirm_destructive":
                return (
                    f"{proposal.question} ({proposal.reason})"
                    if proposal.question
                    else proposal.reason
                )
            case "clarify":
                return proposal.question or proposal.reason
            case "status":
                return self.status_summary()
            case "message_worker":
                assert proposal.worker_id is not None
                await self.send(proposal.worker_id, proposal.message)
                self.selected_worker_id = proposal.worker_id
                worker = self._require_worker(proposal.worker_id)
                return f"Sent to {worker.title}. {proposal.reason}"
            case "attach_worker":
                assert proposal.worker_id is not None
                try:
                    attachment = await self.attach(proposal.worker_id)
                except AttachError as exc:
                    return f"Cannot attach: {exc}"
                self.selected_worker_id = proposal.worker_id
                worker = self._require_worker(proposal.worker_id)
                return (
                    f"{worker.title} is yours. Press Ctrl+E to enter it here, or run:\n"
                    f"{attachment.shell_hint}"
                )
            case "resume_run":
                assert proposal.job_id is not None
                run = self.store.active_run(proposal.job_id)
                if run is None:
                    return "That job has no paused workflow run."
                resumed = await self.resume_run(run.id)
                return self._run_reply(resumed)
            case "start_workflow":
                name = proposal.workflow or "ask-question"
                if get_workflow(name).is_composite:
                    assert proposal.job_id is not None
                    run = await self.start_run(
                        name, job_id=proposal.job_id, request=proposal.message
                    )
                    return self._run_reply(run)
                worker = await self.start_workflow(
                    name,
                    job_id=proposal.job_id,
                    target_worker_id=proposal.worker_id,
                    request=proposal.message,
                )
                self.selected_worker_id = worker.id
                return f"Running {proposal.workflow} on {worker.title}."
            case "new_question_worker":
                assert proposal.repository_id is not None
                definition = get_workflow("ask-question")
                worker = await self.create_worker(
                    role=WorkerRole.QUESTION,
                    title=proposal.title or "Question",
                    prompt=render_template(definition.template, {"request": proposal.message}),
                    job_id=proposal.job_id,
                    repository_id=proposal.repository_id,
                    writable=False,
                    workflow="ask-question",
                )
                self.selected_worker_id = worker.id
                return f"Read-only question worker started (no worktree). {proposal.reason}"
            case "new_job":
                assert proposal.repository_id is not None
                job = self.create_job(
                    title=proposal.title,
                    repository_id=proposal.repository_id,
                    external_ref=proposal.external_ref,
                    ticket_text=proposal.message,
                )
                label = job.external_ref or job.title
                # A one-off named workflow wins; otherwise the repository or user profile.
                name = proposal.workflow or router.DEFAULT_PROFILE
                if name == router.DEFAULT_PROFILE:
                    name = self.resolve_profile(job.repository_id, job)
                if get_workflow(name).is_composite:
                    run = await self.start_run(name, job_id=job.id, request=proposal.message)
                    return f"Started {label} on the {name} workflow. {self._run_reply(run)}"
                worker = await self.start_workflow(name, job_id=job.id, request=proposal.message)
                self.selected_worker_id = worker.id
                return f"Started {label} in a new job. Planning is in progress."
        return proposal.reason

    def _run_reply(self, run: WorkflowRun) -> str:
        """One line describing where a run stands, for the manager pane."""
        worker = self.store.get_worker(run.current_worker_id) if run.current_worker_id else None
        if worker is not None:
            self.selected_worker_id = worker.id
            return f"Step {run.step_index + 1}: {worker.workflow} on {worker.title}."
        if run.status is RunStatus.COMPLETED:
            return "Every applicable step is done."
        return run.detail or f"{run.workflow} is {run.status.value}."

    def status_summary(self) -> str:
        items = self.list_attention_items()
        if not items:
            active = [
                w
                for w in self.store.list_workers()
                if w.status in (WorkerStatus.WORKING, WorkerStatus.STARTING)
            ]
            return f"Nothing needs you. {len(active)} worker(s) still working."
        counts: dict[str, int] = {}
        for item in items:
            counts[item.kind.value.replace("_", " ")] = counts.get(
                item.kind.value.replace("_", " "), 0
            ) + 1
        parts = ", ".join(f"{count} {label}" for label, count in counts.items())
        return f"{len(items)} worker(s) need attention: {parts}."

    # ------------------------------------------------------------------ helpers

    def _require_worker(self, worker_id: UUID) -> Worker:
        worker = self.store.get_worker(worker_id)
        if worker is None:
            raise SessionManagerError(f"Worker {worker_id} does not exist.")
        return worker

    def _record(self, worker: Worker, role: str, text: str) -> None:
        self.store.add_transcript(TranscriptMessage(worker_id=worker.id, role=role, text=text))

    def _set_status(
        self, worker: Worker, status: WorkerStatus, waiting_for: str | None = ...  # type: ignore[assignment]
    ) -> Worker:
        fresh = self.store.get_worker(worker.id) or worker
        assert_worker_transition(fresh.status, status)
        return self._force_status(
            fresh, status, fresh.waiting_for if waiting_for is ... else waiting_for
        )

    def _force_status(self, worker: Worker, status: WorkerStatus, waiting_for: str | None) -> Worker:
        worker.status = status
        worker.waiting_for = waiting_for
        worker.updated_at = now()
        return self.store.save_worker(worker)

    def _worktree_path(self, worker: Worker) -> Path | None:
        if worker.worktree_id:
            worktree = self.store.get_worktree(worker.worktree_id)
            if worktree:
                return worktree.path
        return worker.cwd

    def _head(self, worker: Worker) -> str | None:
        path = self._worktree_path(worker)
        try:
            return runner.head_commit(path) if path and path.exists() else None
        except GitError:
            return None

    def _tree(self, worker: Worker) -> str | None:
        path = self._worktree_path(worker)
        try:
            return runner.tree_hash(path) if path and path.exists() else None
        except GitError:
            return None


def _last_question(text: str) -> str:
    """The concise reason a worker is blocked: its final question or last line."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if line.endswith("?"):
            return line[:200]
    return (lines[-1] if lines else "Waiting for the user.")[:200]
