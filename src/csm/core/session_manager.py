"""The orchestration service.

UI actions call this; this emits events; persistence, status, and the attention queue
follow from those events. Every Git and worktree invariant is enforced here in ordinary
Python -- never by asking a model to behave.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from csm.agents.backend import WorkerBackend, WorkerEvent, WorkerSpec
from csm.agents.prompts import PROMPT_POLICY_VERSION, compose_worker_prompt
from csm.config import Config
from csm.core.transitions import assert_worker_transition
from csm.domain import events as ev
from csm.domain.contracts import (
    BehaviorContract,
    CommentResolutionReport,
    ImplementationContract,
    ReviewReport,
    VerificationReport,
    extract_json_block,
)
from csm.domain.enums import (
    READ_ONLY_ROLES,
    ArtifactType,
    AttentionKind,
    JobStage,
    Verbosity,
    WorkerRole,
    WorkerStatus,
)
from csm.domain.models import (
    Artifact,
    AttentionItem,
    Decision,
    Event,
    Job,
    Repository,
    TranscriptMessage,
    Worker,
    WorkflowExecution,
    Worktree,
    now,
)
from csm.gitops import runner
from csm.gitops.runner import GitError
from csm.gitops.worktrees import CleanupDecision, WorktreeSafetyError, WorktreeService
from csm.routing import router
from csm.routing.router import RouteError, RouteProposal, RoutingState
from csm.storage.store import Store
from csm.workflows.freshness import (
    BEHAVIORAL_ARTIFACTS,
    CodeChange,
    GitSnapshot,
    artifacts_invalidated_by,
    classify_change,
    is_fresh,
    relineage,
)
from csm.workflows.registry import (
    WorkflowDefinition,
    WorkflowError,
    get_workflow,
    render_template,
    validate_for_role,
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
        self._pending_change: dict[UUID, tuple[GitSnapshot, WorkflowDefinition]] = {}
        self._listeners: list[Callable[[Event], None]] = []

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
        return self.store.add_repository(repo)

    def list_repositories(self) -> list[Repository]:
        return self.store.list_repositories()

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

    async def _start_backend(self, worker: Worker, prompt: str, resume: bool = False) -> None:
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
        )
        if prompt:
            self._record(worker, "user", prompt)
        try:
            handle = (
                await self.backend.resume(spec) if resume else await self.backend.start(spec)
            )
        except Exception as exc:
            self._set_status(worker, WorkerStatus.FAILED, waiting_for=f"Backend error: {exc}")
            self.emit(
                ev.WORKER_FAILED, worker_id=worker.id, job_id=worker.job_id, summary=str(exc)
            )
            raise SessionManagerError(f"Could not start worker {worker.title!r}: {exc}") from exc
        if handle.session_id:
            worker.session_id = handle.session_id
        self._set_status(worker, WorkerStatus.WORKING)
        self.emit(
            ev.WORKER_RESUMED if resume else ev.WORKER_STARTED,
            worker_id=worker.id,
            job_id=worker.job_id,
            summary=worker.title,
        )
        self._pumps[worker.id] = asyncio.create_task(self._pump(worker.id))

    def _workflow_policy(self, workflow: str | None) -> str | None:
        definition = self._definition(workflow)
        if definition is None:
            return None
        return f"Current workflow: {definition.name}. {definition.description.strip()}"

    # --------------------------------------------------------------- messaging

    async def send(self, worker_id: UUID, message: str) -> None:
        worker = self._require_worker(worker_id)
        if worker.status in (WorkerStatus.STOPPED, WorkerStatus.DISCONNECTED):
            raise SessionManagerError(
                f"Worker {worker.title!r} is {worker.status.value}; start a replacement instead."
            )
        self._record(worker, "user", message)
        self._resolve_attention(worker)
        self._set_status(worker, WorkerStatus.WORKING, waiting_for=None)
        self._snapshot_before_change(worker)
        await self.backend.send(worker_id, message)

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
        return worker

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
        """Each workflow declares the stage it moves its job to; unset means no change."""
        if job is not None and definition.stage is not None:
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

    async def stop_worker(self, worker_id: UUID) -> None:
        worker = self._require_worker(worker_id)
        pump = self._pumps.pop(worker_id, None)
        await self.backend.stop(worker_id)
        if pump is not None:
            pump.cancel()
        self._set_status(worker, WorkerStatus.STOPPED, waiting_for=None)
        self.emit(ev.WORKER_STOPPED, worker_id=worker.id, job_id=worker.job_id, summary=worker.title)

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
        from csm.routing.attention import prioritize

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
            case "text":
                self._record(worker, "assistant", event.text)
                self.emit(ev.WORKER_OUTPUT, worker_id=worker.id, job_id=worker.job_id)
            case "tool":
                self._record(worker, "tool", f"[{event.text}]")
            case "helper":
                worker.active_helpers = int(event.data.get("active", 0))
                self.store.save_worker(worker)
            case "permission":
                self._set_status(worker, WorkerStatus.BLOCKED, waiting_for=event.text)
                self.raise_attention(
                    worker, AttentionKind.PERMISSION_REQUIRED, event.text, event.text
                )
                self.emit(ev.WORKER_PERMISSION_REQUIRED, worker_id=worker.id, job_id=worker.job_id)
            case "blocked":
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
            case "result":
                self._finish_turn(worker, event.text)
                if event.data.get("is_error"):
                    self._set_status(worker, WorkerStatus.FAILED, waiting_for="Turn failed.")
                    self.raise_attention(
                        worker, AttentionKind.WORKER_FAILED, "The worker's turn failed."
                    )
                    self.emit(ev.WORKER_FAILED, worker_id=worker.id, job_id=worker.job_id)
                else:
                    self._set_status(worker, WorkerStatus.IDLE, waiting_for=None)
                    self.emit(ev.WORKER_COMPLETED, worker_id=worker.id, job_id=worker.job_id)
            case "failed":
                self._set_status(worker, WorkerStatus.FAILED, waiting_for=event.text[:200])
                self.raise_attention(worker, AttentionKind.WORKER_FAILED, event.text[:200])
                self.emit(
                    ev.WORKER_FAILED, worker_id=worker.id, job_id=worker.job_id, summary=event.text
                )
            case "stopped":
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
        if not worker.writable:
            return
        definition = self._definition(worker.workflow)
        if definition is None or not definition.mutates_code:
            return
        head, tree = self._head(worker), self._tree(worker)
        if head and tree:
            self._pending_change[worker.id] = (GitSnapshot(head, tree), definition)

    def _apply_invalidation(self, worker: Worker, job: Job | None) -> None:
        pending = self._pending_change.pop(worker.id, None)
        if pending is None or job is None:
            return
        before, definition = pending
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
        targets = set(definition.invalidates) | artifacts_invalidated_by(change)
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
        """Restore durable state on startup; resume what can be resumed, mark the rest."""
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
            if not worker.session_id:
                self._force_status(
                    worker,
                    WorkerStatus.DISCONNECTED,
                    "No session id was captured, so this session cannot be resumed. Start a "
                    "replacement seeded from the stored job artifacts.",
                )
                notes.append(f"{worker.title}: no session id")
                continue
            try:
                await self._start_backend(worker, prompt="", resume=True)
                self._force_status(worker, WorkerStatus.IDLE, None)
                notes.append(f"{worker.title}: resumed")
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
            case "start_workflow":
                worker = await self.start_workflow(
                    proposal.workflow or "ask-question",
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
                    prompt=definition.template.format(request=proposal.message),
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
                worker = await self.start_workflow(
                    proposal.workflow or "plan-feature", job_id=job.id, request=proposal.message
                )
                self.selected_worker_id = worker.id
                label = job.external_ref or job.title
                return f"Started {label} in a new job. Planning is in progress."
        return proposal.reason

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
