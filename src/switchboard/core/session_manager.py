"""The orchestration service.

UI actions call this; this emits events; persistence, status, and the attention queue
follow from those events. Every Git and worktree invariant is enforced here in ordinary
Python -- never by asking a model to behave.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import yaml
from pydantic import ValidationError

from switchboard.agents.attach import AttachError, Attachment
from switchboard.agents.backend import (
    WorkerBackend,
    WorkerBusyError,
    WorkerEvent,
    WorkerNotReadyError,
    WorkerSpec,
)
from switchboard.agents.prompts import PROMPT_POLICY_VERSION, compose_worker_prompt
from switchboard.config import Config, user_workflows_dir
from switchboard.core import evidence, lineage
from switchboard.core.errors import SessionManagerError
from switchboard.core.evidence import CompletionReport
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
    COMPLETE_STAGE,
    DEFAULT_WRITABLE_ROLES,
    TERMINAL_WORKER_STATUSES,
    ArtifactType,
    AttentionKind,
    NativeTurnStatus,
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
from switchboard.workflows.registry import (
    REPO_WORKFLOW_DIR,
    Approval,
    WorkerMode,
    WorkflowDefinition,
    WorkflowStep,
    builtin_names,
    find_workflow,
    get_workflow,
    reload_workflows,
    render_template,
    validate_for_role,
    workflow_names,
)

log = logging.getLogger(__name__)


#: Fallback for a worker started with a role but no workflow (a bare `create_worker`).
ROLE_ARTIFACTS: dict[WorkerRole, frozenset[ArtifactType]] = {
    WorkerRole.PLANNER: frozenset({ArtifactType.IMPLEMENTATION_CONTRACT}),
    WorkerRole.VERIFIER: frozenset({ArtifactType.VERIFICATION}),
    WorkerRole.REVIEWER: frozenset({ArtifactType.REVIEW}),
    WorkerRole.REVIEW_COMMENTS: frozenset({ArtifactType.COMMENT_RESOLUTIONS}),
}


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
        self._run_locks: dict[UUID, asyncio.Lock] = {}

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

    def update_job_stage(self, job: Job, stage: str) -> Job:
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

        # Only a bare worker reaches this; a workflow always states its own writability.
        if writable is None:
            writable = role in DEFAULT_WRITABLE_ROLES

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
            if job is not None and job.authoritative_worktree_id is None:
                job.authoritative_worktree_id = worktree.id
                job.updated_at = now()
                self.store.save_job(job)
        elif job is not None:
            # Read-only workers observe the job's writable worktree when one exists, so a
            # reviewer or verifier sees the change under review without owning it.
            worker.cwd = lineage.inspection_path(self.store, job) or repo.root_path

        self.store.save_worker(worker)
        self.store.save_runtime(self._new_runtime(worker, generation=1))
        if job is not None:
            definition = find_workflow(workflow)
            if definition is not None:
                # Reserve a composite step before native launch/send. A crash from this
                # point onward recovers this exact worker instead of dispatching twice.
                self._adopt_into_run(job, worker, definition)
        lineage.snapshot_before_turn(self.store, worker)
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
        lineage.snapshot_before_turn(self.store, worker)
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

    def set_authoritative_worktree(self, job_id: UUID, worktree_id: UUID) -> Job:
        """Explicitly choose the one job lineage inspected by every downstream gate."""
        return lineage.set_authoritative(self.store, job_id, worktree_id)

    async def _start_backend(
        self, worker: Worker, prompt: str, resume: bool = False, adopt: bool = False
    ) -> None:
        runtime = self.store.current_runtime(worker.id)
        if runtime is None:
            raise SessionManagerError(f"Worker {worker.title!r} has no runtime instance.")
        if not adopt:
            runtime.process_state = RuntimeProcessState.STARTING
            runtime.updated_at = now()
            self.store.save_runtime(runtime)
        spec = self._worker_spec(
            worker,
            prompt,
            runtime_id=runtime.id,
            runtime_generation=runtime.generation,
            resume=resume,
        )
        if prompt:
            self._record(worker, "user", prompt)
            if not adopt:
                self.store.set_preference(f"worker.pending_startup_prompt:{worker.id}", prompt)
        try:
            handle = (
                await self.backend.adopt(spec)
                if adopt
                else await self.backend.resume(spec)
                if resume
                else await self.backend.start(spec)
            )
        except Exception as exc:
            try:
                observation = await self.backend.observe(worker.id)
            except Exception:
                observation = None
            startup_alive = observation is not None and observation.exists
            # The launch persists the substrate identity before it can time out waiting
            # for SessionStart, so the pre-launch snapshot is stale. Writing it back would
            # erase the tmux target and refuse the very Ctrl+E this failure asks for.
            runtime = self.store.get_runtime(runtime.id) or runtime
            runtime.process_state = (
                runtime.process_state
                if runtime.process_state is RuntimeProcessState.READY
                else RuntimeProcessState.STARTING
                if startup_alive
                else RuntimeProcessState.EXITED
            )
            runtime.updated_at = now()
            self.store.save_runtime(runtime)
            waiting_for = (
                "Native Claude startup needs human attention. Press Ctrl+E to enter this "
                "session and handle "
                f"workspace trust, login, or another startup prompt. ({exc})"
                if startup_alive
                else f"Backend error: {exc}"
            )
            self._set_status(
                worker,
                WorkerStatus.BLOCKED if startup_alive else WorkerStatus.FAILED,
                waiting_for=waiting_for,
            )
            if startup_alive and await self._auto_answer_trust(worker):
                # The user already vouched for this repository's worktrees, so the dialog
                # that stopped the launch is answered and the worker carries on. Without
                # this, every writable worker stops on the same question about a fresh
                # worktree path Switchboard created itself.
                runtime = self.store.get_runtime(runtime.id) or runtime
                if handle_session_id := runtime.claude_session_id:
                    worker.session_id = handle_session_id
                    self.store.save_worker(worker)
                self._resolve_attention(worker, kinds={AttentionKind.PERMISSION_REQUIRED})
                self._force_status(worker, WorkerStatus.WORKING, None)
                self._ensure_pump(worker.id, replace=True)
                self.emit(
                    ev.WORKER_STARTED,
                    worker_id=worker.id,
                    job_id=worker.job_id,
                    summary=worker.title,
                )
                await self.resume_startup(worker.id)
                return
            if startup_alive:
                self.raise_attention(worker, AttentionKind.PERMISSION_REQUIRED, waiting_for)
                # The native session is live and already emitting events. Without a pump
                # nothing observes the turn the user is about to unblock, so the worker
                # would run to completion while the board still reported it starting.
                self._ensure_pump(worker.id)
            self.emit(
                ev.WORKER_FAILED, worker_id=worker.id, job_id=worker.job_id, summary=str(exc)
            )
            raise SessionManagerError(
                f"Could not start worker {worker.title!r}: {exc}", worker_id=worker.id
            ) from exc
        runtime = self.store.get_runtime(runtime.id) or runtime
        if handle.session_id:
            worker.session_id = handle.session_id
            runtime.claude_session_id = handle.session_id
            self.store.save_worker(worker)
        if prompt and not adopt:
            self.store.set_preference(f"worker.pending_startup_prompt:{worker.id}", "")
        runtime.updated_at = now()
        self.store.save_runtime(runtime)
        if not adopt:
            self._set_status(worker, WorkerStatus.WORKING)
        self.emit(
            ev.WORKER_RESUMED if resume else ev.WORKER_STARTED,
            worker_id=worker.id,
            job_id=worker.job_id,
            summary=worker.title,
        )
        # A completed launch means a new backend session object, and a pump binds the one
        # it started with, so an inherited pump would consume nothing. Always replace here.
        self._ensure_pump(worker.id, replace=True)

    async def _auto_answer_trust(self, worker: Worker) -> bool:
        """Answer a trust dialog when the user already vouched for this repository."""
        if not self.repository_trust_granted(worker.repository_id):
            return False
        try:
            await self.answer_workspace_trust(worker.id)
        except SessionManagerError as exc:
            log.info("not auto-answering startup for %s: %s", worker.title, exc)
            return False
        # The dialog is answered; the session still has to reach SessionStart.
        return await self.backend.wait_ready(worker.id)

    def _ensure_pump(self, worker_id: UUID, *, replace: bool = False) -> None:
        """One live consumer of a worker's backend events, however it was started."""
        existing = self._pumps.get(worker_id)
        if existing is not None and not existing.done():
            if not replace:
                return
            existing.cancel()
        self._pumps[worker_id] = asyncio.create_task(self._pump(worker_id))

    def _worker_spec(
        self,
        worker: Worker,
        prompt: str,
        *,
        runtime_id: UUID | None = None,
        runtime_generation: int = 1,
        resume: bool = False,
    ) -> WorkerSpec:
        return WorkerSpec(
            worker_id=worker.id,
            role=worker.role.value,
            cwd=worker.cwd,
            system_prompt_append=compose_worker_prompt(
                worker.role,
                self.config,
                writable=worker.writable,
                verbosity=self.verbosity.get(worker.id, Verbosity.CONCISE),
                workflow_policy=self._workflow_policy(worker.workflow),
                role_policy=self._role_policy(worker.workflow),
            ),
            initial_prompt=prompt,
            model=worker.model,
            writable=worker.writable,
            resume_session_id=worker.session_id if resume else None,
            max_helpers=self.config.subagents.max_concurrent_per_worker,
            claude_executable=self.config.claude.executable,
            env=dict(self.config.claude.env),
            runtime_id=runtime_id,
            runtime_generation=runtime_generation,
        )

    def _new_runtime(self, worker: Worker, *, generation: int) -> RuntimeInstance:
        spec = self._worker_spec(worker, "", runtime_generation=generation)
        return RuntimeInstance(
            agent_id=worker.id,
            agent_kind=RuntimeAgentKind.WORKER,
            generation=generation,
            backend=type(self.backend).__name__,
            claude_session_id=None,
            launch_fingerprint=self.backend.launch_fingerprint(spec),
        )

    def _runtime_fingerprint(self, worker: Worker) -> str:
        return self.backend.launch_fingerprint(self._worker_spec(worker, ""))

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
        definition = find_workflow(workflow)
        if definition is None:
            return None
        return f"Current workflow: {definition.name}. {definition.description.strip()}"

    def _role_policy(self, workflow: str | None) -> str | None:
        """The role policy a workflow declared for a role Switchboard has none for."""
        definition = find_workflow(workflow)
        return definition.role_policy if definition and definition.role_policy else None

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
        self._apply_invalidation(
            worker, self.store.get_job(worker.job_id) if worker.job_id else None
        )
        lineage.snapshot_before_turn(self.store, worker)
        try:
            await self.backend.send(worker_id, message)
        except WorkerBusyError as exc:
            raise SessionManagerError(str(exc)) from exc
        except WorkerNotReadyError as exc:
            # Input raced the runtime rather than failing in flight: the send was refused
            # by a precondition and delivered nothing. Recording WAITING here made that
            # refusal self-fulfilling -- the runtime then failed its own readiness check
            # on every later send, stalling the job for good.
            raise SessionManagerError(
                f"Worker {worker.title!r} is not ready for input yet: {exc}"
            ) from exc
        except Exception as exc:
            self._set_runtime_state(worker.id, RuntimeProcessState.WAITING)
            self._force_status(
                worker, WorkerStatus.DISCONNECTED, f"Could not deliver input: {exc}"
            )
            raise SessionManagerError(
                f"Could not send to worker {worker.title!r}: {exc}"
            ) from exc
        self._record(worker, "user", message)
        self._resolve_attention(worker)
        self._set_status(worker, WorkerStatus.WORKING, waiting_for=None)
        self._unpause_run_of(worker)

    def _clear_approval_gate(self, run: WorkflowRun) -> None:
        """A run that is moving again no longer needs the gate that stopped it.

        `approve_plan` retires its own items, but a gate satisfied by answering a decision
        or by an explicit resume would otherwise stay on the board forever -- the exact
        inverse of the silent-gate bug.
        """
        for item in self.store.list_attention_items():
            if item.job_id == run.job_id and item.kind is AttentionKind.PLAN_APPROVAL:
                item.handled = True
                self.store.save_attention_item(item)

    def _unpause_run_of(self, worker: Worker) -> None:
        """Answering the worker a run stopped on puts the run back in flight."""
        run = self.store.run_for_worker(worker.id)
        if run is not None and run.status in (RunStatus.AWAITING_APPROVAL, RunStatus.BLOCKED):
            self._clear_approval_gate(run)
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
        if job is not None:
            job = lineage.ensure_authoritative(self.store, job)
            self._reconcile_job_git(job)
        self._assert_prerequisites(definition, job)

        if worker is not None:
            validate_for_role(definition.name, worker.role)
            if definition.mutates_code and not worker.writable:
                raise SessionManagerError(
                    f"{definition.name} mutates code but {worker.title!r} is read-only."
                )
            if (
                definition.mutates_code
                and job is not None
                and job.authoritative_worktree_id is not None
                and worker.worktree_id != job.authoritative_worktree_id
            ):
                raise SessionManagerError(
                    f"{worker.title!r} does not own this job's authoritative change lineage."
                )
            if (
                not definition.mutates_code
                and job is not None
                and job.authoritative_worktree_id is not None
                and worker.cwd != lineage.inspection_path(self.store, job)
            ):
                raise SessionManagerError(
                    f"{worker.title!r} observes a different worktree than this job's "
                    "authoritative change lineage. Start a fresh worker."
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
        existing = next(
            (
                candidate
                for candidate in reversed(self.store.list_workers(job.id))
                if candidate.workflow == definition.name
                and candidate.status
                in (WorkerStatus.STARTING, WorkerStatus.WORKING, WorkerStatus.BLOCKED)
            ),
            None,
        )
        if existing is not None:
            raise SessionManagerError(
                f"{job.title!r} already has {definition.name} on {existing.title!r} "
                f"({existing.status.value}). Enter or recover that session instead of "
                "starting a duplicate."
            )
        prompt = self._render(definition, job, request)
        worker = await self.create_worker(
            role=definition.role,
            title=f"{job.external_ref or job.title} · {definition.role.value}",
            prompt=prompt,
            job_id=job.id,
            writable=definition.mutates_code,
            workflow=definition.name,
        )
        self._note_execution(job, worker, definition.name)
        # Worker creation may establish the job's authoritative worktree. Do not save
        # the older in-memory Job while advancing its stage and erase that lineage.
        current_job = self.store.get_job(job.id) or job
        self._advance_stage(current_job, definition)
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
        composite = find_workflow(run.workflow)
        if composite is None or run.step_index >= len(composite.steps):
            return
        if composite.steps[run.step_index].workflow != definition.name:
            return
        run.iterations[str(run.step_index)] = run.iterations.get(str(run.step_index), 0) + 1
        run.current_worker_id = worker.id
        run.current_step_completed = False
        run.completion_turn_id = None
        run.human_intervened = False
        run.status = RunStatus.RUNNING
        run.updated_at = now()
        self.store.save_run(run)

    # ------------------------------------------------------- composite workflows

    def resolve_composite_workflow(
        self, repository_id: UUID | None, job: Job | None = None
    ) -> str:
        """Which composite workflow a job should follow.

        Precedence: the job's own, then the repository preference, then the user's
        configured default. A one-off instruction beats all of them by naming a workflow
        explicitly, which never reaches this method.
        """
        if job is not None and job.composite_workflow:
            return job.composite_workflow
        if repository_id is not None:
            preference = self.store.get_preference(f"composite_workflow:{repository_id}")
            if preference:
                return preference
        return self.config.default_composite_workflow

    def set_repository_composite_workflow(self, repository_id: UUID, name: str) -> None:
        get_workflow(name)  # refuse to store a workflow that does not exist
        self.store.set_preference(f"composite_workflow:{repository_id}", name)

    async def start_run(self, workflow_name: str, *, job_id: UUID, request: str = "") -> WorkflowRun:
        """Begin a composite workflow over a job and start its first applicable step."""
        definition = get_workflow(workflow_name)
        if not definition.is_composite:
            raise SessionManagerError(f"{definition.name} is not a composite workflow.")
        job = self.store.get_job(job_id)
        if job is None:
            raise SessionManagerError(f"Job {job_id} does not exist.")
        job = lineage.ensure_authoritative(self.store, job)
        existing = self.store.active_run(job.id)
        if existing is not None:
            raise SessionManagerError(
                f"{job.title!r} is already running {existing.workflow} "
                f"(step {existing.step_index + 1}, {existing.status.value})."
            )
        job.composite_workflow = definition.name
        self.store.save_job(job)
        run = WorkflowRun(
            job_id=job.id,
            workflow=definition.name,
            request=request,
            head_at_start=lineage.job_head(self.store, job),
        )
        self.store.save_run(run)
        self.emit(
            ev.RUN_STARTED, job_id=job.id, summary=f"{definition.name} started for {job.title!r}."
        )
        return await self.advance_run(run.id)

    async def advance_run(self, run_id: UUID) -> WorkflowRun:
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            return await self._advance_run(run_id)

    async def _advance_run(self, run_id: UUID) -> WorkflowRun:
        """Move a run to its next applicable step, or pause it and say why.

        Called once when a run starts and once each time its current worker finishes a
        turn. Every decision here reads stored state, so a run resumed after a restart
        behaves identically.
        """
        run = self.store.get_run(run_id)
        if run is None:
            raise SessionManagerError(f"Run {run_id} does not exist.")
        if run.status is not RunStatus.RUNNING:
            return run
        job = self.store.get_job(run.job_id)
        if job is not None:
            self._reconcile_job_git(job)
        definition = find_workflow(run.workflow)
        if job is None or definition is None or not definition.is_composite:
            return self._pause_run(run, RunStatus.FAILED, "Its workflow or job no longer exists.")
        steps = definition.steps

        # The step that just finished decides whether the run may continue.
        if run.current_worker_id is not None:
            if not run.current_step_completed:
                return run
            if run.step_index < len(steps) and not self._approval_satisfied(run, steps[run.step_index], job):
                return self._await_approval(
                    run,
                    f"Step {run.step_index + 1} ({steps[run.step_index].workflow}) needs your approval.",
                )
            run.current_worker_id = None
            run.current_step_completed = False
            run.completion_turn_id = None
            run.human_intervened = False
            run.step_index += 1

        while True:
            head = lineage.job_head(self.store, job)
            if run.step_index >= len(steps):
                repeat = self._repeat_target(run, definition, job, head)
                if repeat is None:
                    run.status = RunStatus.COMPLETED
                    run.detail = "Every applicable step is done."
                    run.updated_at = now()
                    self.store.save_run(run)
                    self.emit(ev.RUN_COMPLETED, job_id=job.id, summary=run.detail)
                    # An unfinished run is itself a completion blocker, so the job can
                    # only be judged finished once the run stops being one.
                    self._check_completion(
                        self.store.get_job(job.id) or job, self._gate_worker(run)
                    )
                    return run
                run.step_index = repeat
            step = steps[run.step_index]
            step_definition = find_workflow(step.workflow)
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
                return self._await_approval(run, str(exc))

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
                # A worker blocked at native startup is still this step's worker. Without
                # the link the run can never be unblocked by answering it, and a resume
                # only meets the duplicate-worker refusal again.
                if exc.worker_id is not None:
                    run = self.store.get_run(run.id) or run
                    run.iterations[str(run.step_index)] = used + 1
                    run.current_worker_id = exc.worker_id
                    run.current_step_completed = False
                    run.completion_turn_id = None
                    run.human_intervened = False
                    self.store.save_run(run)
                return self._pause_run(run, RunStatus.BLOCKED, str(exc))
            # `start_workflow` adopts the worker into this step; only fill in if it did not.
            run = self.store.get_run(run.id) or run
            if run.current_worker_id is None:
                run.iterations[str(run.step_index)] = used + 1
                run.current_worker_id = worker.id
                run.current_step_completed = False
                run.completion_turn_id = None
                run.human_intervened = False
                run.updated_at = now()
                self.store.save_run(run)
            return run

    def _await_approval(self, run: WorkflowRun, detail: str) -> WorkflowRun:
        """Stop for the user *and* say so in the attention queue.

        A planner that ends its turn cleanly instead of asking a question resolves its own
        attention, so an approval gate reached this way used to leave the board reporting
        `Nothing needs you` while the run waited on the one person who could unblock it.
        """
        paused = self._pause_run(run, RunStatus.AWAITING_APPROVAL, detail)
        worker = self._gate_worker(run)
        if worker is not None:
            self._raise_attention_once(worker, AttentionKind.PLAN_APPROVAL, detail)
        return paused

    def _gate_worker(self, run: WorkflowRun) -> Worker | None:
        """The worker whose session explains this gate, or None rather than a wrong one.

        Entering an attention item opens that worker's session, so pointing the gate at an
        unrelated worker the user happens to have started sends them somewhere that never
        wrote the plan they are being asked to approve.
        """
        if run.current_worker_id is not None:
            return self.store.get_worker(run.current_worker_id)
        definition = find_workflow(run.workflow)
        if definition is None:
            return None
        produced = {step.workflow for step in definition.steps}
        candidates = [
            w
            for w in self.store.list_workers(run.job_id)
            if w.status not in TERMINAL_WORKER_STATUSES and w.workflow in produced
        ]
        return candidates[-1] if candidates else None

    def _raise_attention_once(self, worker: Worker, kind: AttentionKind, reason: str) -> None:
        """Raise attention the user has not already been shown for this worker."""
        open_kinds = {
            item.kind
            for item in self.store.attention_items_for_worker(worker.id)
            if not item.handled
        }
        if kind not in open_kinds:
            self.raise_attention(worker, kind, reason, reason)

    def _approval_satisfied(self, run: WorkflowRun, step: WorkflowStep, job: Job) -> bool:
        """Whether the user has given the approval this step asks for."""
        if step.approval is Approval.NONE:
            return True
        blocking = has_blocking_decisions(self.store, job)
        if step.approval is Approval.ONLY_IF_DECISIONS and not blocking:
            return True
        if blocking:
            return False
        step_definition = find_workflow(step.workflow)
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
            step_definition = find_workflow(step.workflow)
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
            and (
                not definition.mutates_code
                or job.authoritative_worktree_id is None
                or w.worktree_id == job.authoritative_worktree_id
            )
            and (
                definition.mutates_code
                or job.authoritative_worktree_id is None
                or w.cwd == lineage.inspection_path(self.store, job)
            )
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
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            return await self._resume_run(run_id)

    async def _resume_run(self, run_id: UUID) -> WorkflowRun:
        run = self.store.get_run(run_id)
        if run is None:
            raise SessionManagerError(f"Run {run_id} does not exist.")
        if run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            raise SessionManagerError(f"That run already {run.status.value}.")
        if run.status is RunStatus.RUNNING:
            return run
        grant_approval = (
            run.status is RunStatus.AWAITING_APPROVAL and run.current_step_completed
        )
        if run.current_worker_id is not None and (
            not run.current_step_completed or run.human_intervened
        ):
            worker = self.store.get_worker(run.current_worker_id)
            runtime = self.store.current_runtime(run.current_worker_id)
            if worker is None or runtime is None or runtime.owner is RuntimeOwner.HUMAN:
                raise SessionManagerError(
                    "The current step still needs conservative reconciliation before it can resume."
                )
            turns = self.store.list_native_turns(runtime.id)
            if run.human_intervened or (turns and turns[-1].human_intervened):
                # Explicit resume is the user's reconciliation decision. The tainted
                # attempt never counts against the bound and its output never advances;
                # rerun the same workflow from durable contracts on the canonical tree.
                used = run.iterations.get(str(run.step_index), 0)
                run.iterations[str(run.step_index)] = max(0, used - 1)
                run.current_worker_id = None
                run.current_step_completed = False
                run.completion_turn_id = None
                run.human_intervened = False
            else:
                raise SessionManagerError(
                    "The current step has no trusted completion. Wait for it to finish or "
                    "intervene explicitly before resuming."
                )
        if grant_approval and run.step_index not in run.approved_steps:
            run.approved_steps = [*run.approved_steps, run.step_index]
        self._clear_approval_gate(run)
        run.status = RunStatus.RUNNING
        self.store.save_run(run)
        return await self._advance_run(run.id)

    def _pause_run_of(self, worker: Worker, status: RunStatus, detail: str) -> None:
        run = self.store.run_for_worker(worker.id)
        if run is not None and run.status is RunStatus.RUNNING:
            self._pause_run(run, status, detail)

    def _schedule_run_advance(self, worker: Worker) -> None:
        """Continue the run this worker is a step of, once its turn has finished."""
        if worker.job_id is None:
            return
        run = self.store.run_for_worker(worker.id)
        if (
            run is None
            or run.status is not RunStatus.RUNNING
            or run.human_intervened
        ):
            return
        self._spawn(self.advance_run(run.id))

    def _complete_run_step(self, worker: Worker, turn_id: str | None) -> None:
        """Durably authorize advancement after a successful trusted worker result."""
        run = self.store.run_for_worker(worker.id)
        if run is None or run.status is not RunStatus.RUNNING:
            return
        run.current_step_completed = True
        run.completion_turn_id = UUID(turn_id) if turn_id else None
        run.updated_at = now()
        self.store.save_run(run)

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
        if not definition.requires:
            return
        if job is None:
            raise SessionManagerError(
                f"{definition.name} needs a job carrying "
                f"{', '.join(sorted(a.value for a in definition.requires))}."
            )
        for required in sorted(definition.requires, key=lambda a: a.value):
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
        """Each workflow declares the label it moves its job to; unset means no change.

        A label is a description, never a claim. Whether the work is finished is decided
        only by the completion gate, which reads evidence rather than a declaration.
        """
        if job is None or not definition.stage:
            return
        self.update_job_stage(job, definition.stage)

    def _note_execution(self, job: Job | None, worker: Worker, workflow_name: str) -> None:
        self.store.add_workflow_execution(
            WorkflowExecution(
                job_id=job.id if job else None,
                worker_id=worker.id,
                workflow=workflow_name,
                head_commit=lineage.worker_head(self.store, worker),
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
            base, head, commits, diff = lineage.review_inputs(self.store, job)
            values |= {
                "base_commit": base, "head_commit": head, "commits": commits, "diff": diff
            }
        return render_template(definition.prompt, values)

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

    # ------------------------------------------------------- workspace trust

    #: Text native Claude puts on screen while asking whether a directory is trusted.
    #: Only used to refuse when the pane is showing something else -- never to decide
    #: what a session is doing, which remains a hook's job.
    TRUST_DIALOG_MARKERS = ("I trust this folder", "Is this a project you created")

    def repository_trust_granted(self, repository_id: UUID) -> bool:
        return self.store.get_preference(f"trust.repository:{repository_id}", "") == "granted"

    def grant_repository_trust(self, repository_id: UUID, *, confirmed: bool) -> None:
        """Record that the user vouches for worktrees Switchboard makes from this repo.

        Claude stores workspace trust per exact directory, and every writable worker gets
        a fresh worktree path, so without this each new worker stops on the same dialog
        about a directory Switchboard created itself from a repository the user
        registered. This does not weaken the question -- it records the user's answer to
        it once, for a specific repository, instead of asking per worktree.
        """
        if not confirmed:
            raise SessionManagerError(
                "Trusting a repository's Switchboard worktrees needs explicit confirmation."
            )
        if self.store.get_repository(repository_id) is None:
            raise SessionManagerError(f"Repository {repository_id} is not registered.")
        self.store.set_preference(f"trust.repository:{repository_id}", "granted")

    async def answer_workspace_trust(self, worker_id: UUID, *, confirmed: bool = False) -> bool:
        """Answer a startup trust dialog for a worker, if that is genuinely what it is.

        Refuses unless the directory is one Switchboard owns for a registered repository
        and the user has vouched for that repository, so this can never accept a dialog
        about somewhere the user never pointed Switchboard at.
        """
        worker = self._require_worker(worker_id)
        if not (confirmed or self.repository_trust_granted(worker.repository_id)):
            raise SessionManagerError(
                "This repository's worktrees are not trusted yet. Confirm once, or enter "
                "the session with Ctrl+E and answer the prompt yourself."
            )
        if not self.worktrees.is_managed_or_repository_path(
            worker.cwd, self.store.get_repository(worker.repository_id)
        ):
            raise SessionManagerError(
                f"{worker.cwd} is neither this repository nor a worktree Switchboard made; "
                "answer that prompt yourself."
            )
        runtime = self.store.current_runtime(worker_id)
        if runtime is None or runtime.process_state is not RuntimeProcessState.STARTING:
            raise SessionManagerError(
                f"{worker.title!r} is not waiting on a startup prompt."
            )
        try:
            pane = self.backend.capture(worker_id)
        except Exception as exc:
            raise SessionManagerError(f"Could not read that session: {exc}") from exc
        if not any(marker in pane for marker in self.TRUST_DIALOG_MARKERS):
            raise SessionManagerError(
                "That session is not showing a workspace-trust prompt. Enter it with "
                "Ctrl+E to see what it is waiting for."
            )
        self.grant_repository_trust(worker.repository_id, confirmed=True)
        await self.backend.answer_startup_dialog(worker_id)
        self.emit(
            ev.WORKSPACE_TRUSTED,
            worker_id=worker.id,
            job_id=worker.job_id,
            summary=f"Answered the workspace-trust prompt for {worker.cwd}.",
        )
        return True

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

        Entry switches ownership and attaches to the exact tmux process without interrupting
        an active turn. Until `detach`, `send` refuses, so Switchboard cannot become a second
        writer. Any composite run pauses in a resumable state while the user has control.
        """
        worker = self._require_worker(worker_id)
        runtime = self.store.current_runtime(worker.id)
        if runtime is None:
            raise AttachError("This worker has no durable runtime instance.")
        spec = self._worker_spec(
            worker,
            "",
            runtime_id=runtime.id,
            runtime_generation=runtime.generation,
        )
        attachment = self.backend.attachment(spec, self._attach_note(worker))
        lineage.snapshot_before_turn(self.store, worker)
        runtime = self.store.current_runtime(worker.id) or runtime
        runtime.owner = RuntimeOwner.HUMAN
        runtime.updated_at = now()
        self.store.save_runtime(runtime)
        self._pause_run_of(worker, RunStatus.BLOCKED, "The user attached to this worker.")
        run = self.store.run_for_worker(worker.id)
        if run is not None:
            run.human_intervened = True
            run.updated_at = now()
            self.store.save_run(run)
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

    def detach(self, worker_id: UUID, *, composer_cleared: bool = False) -> Worker:
        """The user has left the session. Switchboard may drive it again; the run stays paused.

        The run is deliberately not resumed here. The user has just been editing in that
        worktree by hand, so whether the ritual should carry on from where it stopped is
        a judgement only they can make -- `resume_run` is how they say yes.
        """
        worker = self._require_worker(worker_id)
        if not composer_cleared:
            raise SessionManagerError(
                "Clear Claude's composer and explicitly confirm it before manager handback."
            )
        self.backend.release_human(worker_id, composer_cleared=composer_cleared)
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
        # Answering a native permission prompt by hand is the whole point of entering, so
        # a worker whose runtime became ready must not stay BLOCKED. It would keep a stale
        # reason on the board and, being non-terminal, make its own step unreplayable: the
        # replay start is refused as a duplicate workflow. Only READY says the prompt was
        # answered; a user who looks at a STARTING trust prompt and leaves it alone must
        # keep the block, and a dead runtime is not this method's to reinterpret.
        if (
            worker.status is WorkerStatus.BLOCKED
            and runtime is not None
            and runtime.process_state is RuntimeProcessState.READY
        ):
            self._force_status(worker, WorkerStatus.IDLE, None)
            self._resolve_attention(worker, kinds={AttentionKind.PERMISSION_REQUIRED})
        # Entering a worker clears its attention so auto-advance does not bounce straight
        # back. An approval gate is durable run state rather than a transient notice, so
        # it has to return to the board when the user leaves.
        run = self.store.run_for_worker(worker.id)
        if run is not None and run.status is RunStatus.AWAITING_APPROVAL:
            self._raise_attention_once(
                worker, AttentionKind.PLAN_APPROVAL, run.detail or "This run needs your approval."
            )
        self._record(worker, "system", "[the user left this session]")
        return worker

    async def resume_startup(self, worker_id: UUID) -> bool:
        """Deliver an initial prompt delayed by native trust/login startup."""
        worker = self._require_worker(worker_id)
        key = f"worker.pending_startup_prompt:{worker.id}"
        prompt = self.store.get_preference(key, "") or ""
        if not prompt:
            return False
        runtime = self.store.current_runtime(worker.id)
        if runtime is None or runtime.process_state is not RuntimeProcessState.READY:
            return False
        await self.send(worker.id, prompt)
        self.store.set_preference(key, "")
        return True

    def is_attached(self, worker_id: UUID) -> bool:
        runtime = self.store.current_runtime(worker_id)
        return runtime is not None and runtime.owner is RuntimeOwner.HUMAN

    def _attach_note(self, worker: Worker) -> str:
        """Explain shared lineage when entering a read-only observer session."""
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
                f"This read-only worker observes the repository at {worker.cwd}. "
                "Human interaction pauses automatic workflow advancement."
            )
        return (
            f"This read-only worker observes {owner.title}'s authoritative worktree. "
            "Human interaction pauses automatic workflow advancement; that worker retains "
            "lineage ownership."
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

    def _resolve_attention(
        self, worker: Worker, *, kinds: set[AttentionKind] | None = None
    ) -> None:
        for item in self.store.attention_items_for_worker(worker.id):
            if kinds is not None and item.kind not in kinds:
                continue
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
        hook_id = event.data.get("hook_event_id")
        with self.store.transaction():
            if hook_id:
                event_id = UUID(hook_id)
                if self.store.worker_hook_delivered(event_id):
                    return
            self._apply_unchecked(event)
            if hook_id:
                self.store.mark_worker_hook_delivered(UUID(hook_id))

    def _apply_unchecked(self, event: WorkerEvent) -> None:
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
                state = event.data.get("process_state")
                if state:
                    self._set_runtime_state(worker.id, RuntimeProcessState(state))
            case "text":
                self._set_runtime_state(worker.id, RuntimeProcessState.TURN_ACTIVE)
                self._record(worker, "assistant", event.text)
                self.emit(ev.WORKER_OUTPUT, worker_id=worker.id, job_id=worker.job_id)
            case "tool":
                self._set_runtime_state(worker.id, RuntimeProcessState.TURN_ACTIVE)
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
                if event.data.get("final_only") and event.text:
                    self._record(worker, "assistant", event.text)
                    self.emit(ev.WORKER_OUTPUT, worker_id=worker.id, job_id=worker.job_id)
                self._finish_turn(worker, event.text)
                # A planner may deliberately stop for decisions after emitting its
                # complete structured contract. That artifact is a completed step even
                # though the run must remain approval-gated.
                if extract_json_block(event.text) is not None:
                    self._complete_run_step(worker, event.data.get("turn_id"))
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
                if event.data.get("final_only") and event.text:
                    self._record(worker, "assistant", event.text)
                    self.emit(ev.WORKER_OUTPUT, worker_id=worker.id, job_id=worker.job_id)
                self._resolve_attention(worker)
                if event.data.get("is_error"):
                    self._apply_invalidation(
                        worker,
                        self.store.get_job(worker.job_id) if worker.job_id else None,
                    )
                else:
                    self._finish_turn(worker, event.text)
                    self._complete_run_step(worker, event.data.get("turn_id"))
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
        if job is not None:
            # Any turn can be the one that finishes the job -- for `rebase` it is a
            # verification, not a review -- so the gate is consulted after every harvest
            # rather than from inside one workflow's handler.
            self._check_completion(self.store.get_job(job.id) or job, worker)

    # --------------------------------------------------------------- artifacts

    def _harvest_artifact(self, worker: Worker, job: Job, text: str) -> None:
        """Turn a worker's fenced JSON block into the artifact its workflow declares.

        Dispatch is on what the workflow says it *produces*, not on its name, so a
        user-defined workflow that produces a verification is harvested like any other.
        """
        block = extract_json_block(text)
        if block is None:
            return
        head = lineage.worker_head(self.store, worker)
        tree = lineage.worker_tree(self.store, worker)
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
        definition = find_workflow(worker.workflow)
        if definition is not None and definition.produces:
            return definition.produces
        return ROLE_ARTIFACTS.get(worker.role, frozenset())

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
        for item in report.evidence:
            item.tested_head = head or ""
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
            self.update_job_stage(job, "fixing")
        else:
            self.emit(ev.REVIEW_PASSED, job_id=job.id, worker_id=worker.id)

    def _sync_criteria_status(self, job: Job, report: VerificationReport) -> None:
        artifact = self.store.latest_artifact(job.id, ArtifactType.BEHAVIOR_CONTRACT)
        if artifact is None:
            return
        behavior = BehaviorContract.model_validate(artifact.body)
        by_id = {e.criterion_id: e for e in report.evidence}
        for criterion in behavior.criteria:
            item = by_id.get(criterion.id)
            if item is None:
                continue
            criterion.status = "passed" if item.status == "passed" else (
                "blocked" if item.status in ("blocked", "not_tested") else "failed"
            )
            if item.limitations:
                criterion.accepted_limitation = "; ".join(item.limitations)
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

    def _apply_invalidation(
        self, worker: Worker, job: Job | None, *, force: bool = False
    ) -> None:
        """Consume this worker's Git baseline, and announce what the change outdated.

        `lineage` decides and persists; emitting stays here, so every path that
        invalidates an artifact leaves the same audit trail and repaints the board.
        """
        outcome = lineage.apply_invalidation(self.store, worker, job, force=force)
        if outcome is not None and outcome.invalidated and job is not None:
            self.emit(
                ev.ARTIFACT_INVALIDATED,
                job_id=job.id,
                worker_id=worker.id,
                summary=f"{outcome.invalidated} artifact(s) invalidated by {outcome.change.value}.",
            )

    def _reconcile_job_git(self, job: Job) -> None:
        """Apply any durable, unfinished Git baselines before trusting run state."""
        for worker in self.store.list_workers(job.id):
            if worker.writable:
                self._apply_invalidation(worker, job)

    # -------------------------------------------------------------- completion

    def _check_completion(self, job: Job, worker: Worker | None) -> CompletionReport:
        """Announce completion once, the first time the job's workflow says it is done.

        Only a job following a workflow is announced. A one-off question or a hand-run
        atomic workflow has no declared definition of done, so calling it finished would
        be Switchboard's opinion rather than a fact -- and putting that on the attention
        queue would make "needs you" mean "does not need you".
        """
        report = self.job_completion(job.id)
        if not report.ready or report.workflow is None:
            return report
        fresh = self.store.get_job(job.id) or job
        if fresh.completed_at is not None:
            return report
        fresh.completed_at = now()
        fresh.stage = COMPLETE_STAGE
        fresh.updated_at = now()
        self.store.save_job(fresh)
        summary = f"{fresh.title} is complete against {report.workflow or 'its own evidence'}."
        if worker is not None:
            self.raise_attention(worker, AttentionKind.WORK_COMPLETE, summary)
        self.emit(
            ev.JOB_COMPLETE,
            job_id=fresh.id,
            worker_id=worker.id if worker else None,
            summary=report.blurb,
        )
        return report

    def job_completion(self, job_id: UUID) -> CompletionReport:
        """Deterministic gate. Every blocker is computed from stored state, not judgment."""
        return evidence.job_completion(self.store, self.config, job_id)

    def verification_blurb(self, job_id: UUID) -> str:
        return evidence.verification_blurb(self.store, job_id)

    # ---------------------------------------------------------------- recovery

    async def recover(self) -> list[str]:
        """Adopt matching live runtimes, reconstruct absent ones, and reject stale ones."""
        notes: list[str] = []
        recreated_workers: set[UUID] = set()
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
                        or runtime.launch_fingerprint != self._runtime_fingerprint(worker)
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
                    job = self.store.get_job(worker.job_id) if worker.job_id else None
                    if runtime.owner is not RuntimeOwner.HUMAN:
                        self._apply_invalidation(worker, job, force=True)
                    if observed.process_state is not None:
                        runtime.process_state = observed.process_state
                        runtime.updated_at = now()
                        self.store.save_runtime(runtime)
                    await self._start_backend(worker, prompt="", adopt=True)
                    action = "adopted"
                    recovered_state = observed.process_state or runtime.process_state
                else:
                    job = self.store.get_job(worker.job_id) if worker.job_id else None
                    self._apply_invalidation(worker, job, force=True)
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
                    recreated_workers.add(worker.id)
                    recovered_state = RuntimeProcessState.READY
                if recovered_state is RuntimeProcessState.TURN_ACTIVE:
                    self._force_status(worker, WorkerStatus.WORKING, None)
                elif recovered_state is RuntimeProcessState.WAITING:
                    self._force_status(
                        worker, WorkerStatus.BLOCKED, worker.waiting_for or "Runtime is waiting."
                    )
                else:
                    self._force_status(worker, WorkerStatus.IDLE, None)
                    self._resolve_attention(
                        worker, kinds={AttentionKind.PERMISSION_REQUIRED}
                    )
                notes.append(f"{worker.title}: {action}")
            except Exception as exc:
                self._force_status(
                    worker,
                    WorkerStatus.DISCONNECTED,
                    f"Could not resume this session: {exc}. Start a replacement seeded from the "
                    "stored job artifacts.",
                )
                notes.append(f"{worker.title}: {exc}")
        for run in self.store.list_runs():
            if run.status is not RunStatus.RUNNING:
                continue
            if run.current_worker_id in recreated_workers and not run.current_step_completed:
                self._pause_run(
                    run,
                    RunStatus.BLOCKED,
                    "The step runtime disappeared before a trusted completion; refusing "
                    "to resend or advance automatically.",
                )
                notes.append(f"{run.workflow}: incomplete step requires reconciliation")
                continue
            if run.current_worker_id is not None and not run.current_step_completed:
                runtime = self.store.current_runtime(run.current_worker_id)
                turns = self.store.list_native_turns(runtime.id) if runtime else []
                if (
                    runtime is not None
                    and runtime.process_state is RuntimeProcessState.READY
                    and turns
                    and turns[-1].status is NativeTurnStatus.PENDING
                ):
                    self._pause_run(
                        run,
                        RunStatus.BLOCKED,
                        "Prompt delivery is uncertain before UserPromptSubmit. Attach, "
                        "clear the composer, and hand control back before replaying it.",
                    )
                    notes.append(f"{run.workflow}: uncertain prompt delivery blocked")
                    continue
            if run.current_worker_id is None or run.current_step_completed:
                await self.advance_run(run.id)
                notes.append(f"{run.workflow}: composite run reconciled")
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
                    prompt=render_template(definition.prompt, {"request": proposal.message}),
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
                # A one-off named workflow wins; otherwise the repository or user default.
                name = proposal.workflow or router.DEFAULT_COMPOSITE_WORKFLOW
                if name == router.DEFAULT_COMPOSITE_WORKFLOW:
                    name = self.resolve_composite_workflow(job.repository_id, job)
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
            incomplete = [job for job in self.store.list_jobs() if job.completed_at is None]
            if incomplete and not active:
                examples = ", ".join(f"{job.title} ({job.stage})" for job in incomplete[:2])
                suffix = "" if len(incomplete) <= 2 else f" and {len(incomplete) - 2} more"
                return (
                    f"Nothing needs you right now, but {len(incomplete)} incomplete job(s) are "
                    f"idle: {examples}{suffix}."
                )
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


def _last_question(text: str) -> str:
    """The concise reason a worker is blocked: its final question or last line."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if line.endswith("?"):
            return line[:200]
    return (lines[-1] if lines else "Waiting for the user.")[:200]
