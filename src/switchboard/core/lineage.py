"""The job's authoritative Git lineage, and what a change to it invalidates.

A job may have several writable workers, but exactly one of their worktrees *is* the
change. That one is the job's authoritative lineage, and it is what reviewers, verifiers,
freshness, and the ready-to-push gate all inspect. Other writable workers stay isolated
and cannot silently become the change under review.

Everything here reads Git and durable state. Nothing asks a model whether the code moved.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from switchboard.core.errors import SessionManagerError
from switchboard.domain.enums import RuntimeOwner
from switchboard.domain.models import Job, Worker, now
from switchboard.gitops import runner
from switchboard.gitops.runner import GitError
from switchboard.storage.store import Store
from switchboard.workflows.freshness import (
    BEHAVIORAL_ARTIFACTS,
    CodeChange,
    GitSnapshot,
    artifacts_invalidated_by,
    classify_change,
    relineage,
)
from switchboard.workflows.registry import find_workflow

# ------------------------------------------------------------------ which worktree


def inspection_path(store: Store, job: Job) -> Path | None:
    """The directory that holds this job's change, or None if it has none yet."""
    if job.authoritative_worktree_id is None:
        return None
    worktree = store.get_worktree(job.authoritative_worktree_id)
    return worktree.path if worktree and worktree.path.exists() else None


def ensure_authoritative(store: Store, job: Job) -> Job:
    """Adopt a job's single writable worktree as its lineage, or fail closed.

    A job created before the lineage was recorded has the answer implied by its one
    writable worker. Two of them is genuinely ambiguous, and guessing would silently
    decide which change gets reviewed.
    """
    if job.authoritative_worktree_id is not None:
        return job
    candidates = [
        worker.worktree_id
        for worker in store.list_workers(job.id)
        if worker.writable and worker.worktree_id is not None
    ]
    if len(candidates) > 1:
        raise SessionManagerError(
            "This job has multiple writable worktrees but no authoritative lineage. "
            "Choose one explicitly before running another workflow."
        )
    if candidates:
        job.authoritative_worktree_id = candidates[0]
        job.updated_at = now()
        store.save_job(job)
    return job


def set_authoritative(store: Store, job_id: UUID, worktree_id: UUID) -> Job:
    """Explicitly choose the one job lineage inspected by every downstream gate."""
    job = store.get_job(job_id)
    worktree = store.get_worktree(worktree_id)
    if job is None or worktree is None:
        raise SessionManagerError("The job or worktree does not exist.")
    owner = (
        store.get_worker(worktree.owner_worker_id)
        if worktree.owner_worker_id is not None
        else None
    )
    if (
        worktree.repository_id != job.repository_id
        or owner is None
        or owner.job_id != job.id
        or not owner.writable
    ):
        raise SessionManagerError(
            "The authoritative lineage must be a writable worktree owned by this job."
        )
    job.authoritative_worktree_id = worktree.id
    job.updated_at = now()
    return store.save_job(job)


# ------------------------------------------------------------------- reading Git


def job_head(store: Store, job: Job) -> str | None:
    return job_head_and_dirty(store, job)[0]


def job_head_and_dirty(store: Store, job: Job) -> tuple[str | None, list[str]]:
    path = inspection_path(store, job)
    if path is None:
        return None, []
    try:
        return runner.head_commit(path), runner.dirty_files(path)
    except GitError:
        return None, []


def worker_path(store: Store, worker: Worker) -> Path | None:
    if worker.worktree_id:
        worktree = store.get_worktree(worker.worktree_id)
        if worktree:
            return worktree.path
    return worker.cwd


def worker_head(store: Store, worker: Worker) -> str | None:
    path = worker_path(store, worker)
    try:
        return runner.head_commit(path) if path and path.exists() else None
    except GitError:
        return None


def worker_tree(store: Store, worker: Worker) -> str | None:
    path = worker_path(store, worker)
    try:
        return runner.tree_hash(path) if path and path.exists() else None
    except GitError:
        return None


def review_inputs(store: Store, job: Job) -> tuple[str, str, str, str]:
    """Base, head, commit list, and diff for the change a reviewer is being handed."""
    path = inspection_path(store, job)
    if path is None:
        return "", "", "(no worktree)", "(no diff available)"
    try:
        base = runner.run_git(path, "merge-base", job.base_ref, "HEAD").out
        head = runner.head_commit(path)
        commits = "\n".join(runner.commits_between(path, base, head)) or "(no commits yet)"
        return base, head, commits, runner.diff(path, base, head) or "(empty diff)"
    except GitError as exc:
        return "", "", f"(git error: {exc})", "(no diff available)"


# ------------------------------------------------------------------ invalidation


@dataclass(frozen=True)
class Invalidation:
    """What applying a writable worker's Git baseline did to the job's artifacts."""

    change: CodeChange
    head: str
    invalidated: int


def snapshot_before_turn(store: Store, worker: Worker) -> None:
    """Record where Git stood before a writable worker takes a turn.

    Any turn a writable worker takes can change the tree, whatever workflow it is
    running, so the snapshot is taken from writability rather than from intent. The
    baseline is durable runtime state: a controller that dies mid-turn still learns on
    restart that the code moved.
    """
    if not worker.writable:
        return
    head, tree = worker_head(store, worker), worker_tree(store, worker)
    if not head or not tree:
        return
    runtime = store.current_runtime(worker.id)
    if (
        runtime is not None
        and runtime.git_head_before_turn is None
        and runtime.git_tree_before_turn is None
    ):
        runtime.git_head_before_turn = head
        runtime.git_tree_before_turn = tree
        runtime.updated_at = now()
        store.save_runtime(runtime)


def apply_invalidation(
    store: Store, worker: Worker, job: Job | None, *, force: bool = False
) -> Invalidation | None:
    """Consume a writable worker's Git baseline and stale whatever the change outdates.

    `force` is for the boundaries where the human's edit is complete and observed --
    detach and recovery. Without it, a baseline is left alone while the runtime is
    human-owned, because an interrupt completion can arrive after ownership changed and
    must not consume a baseline that now describes somebody else's unfinished work.
    """
    runtime = store.current_runtime(worker.id)
    if (
        runtime is None
        or runtime.git_head_before_turn is None
        or runtime.git_tree_before_turn is None
    ):
        return None
    if runtime.owner is RuntimeOwner.HUMAN and not force:
        return None
    before = GitSnapshot(runtime.git_head_before_turn, runtime.git_tree_before_turn)
    runtime.git_head_before_turn = None
    runtime.git_tree_before_turn = None
    runtime.updated_at = now()
    store.save_runtime(runtime)
    if job is None:
        return None
    head, tree = worker_head(store, worker), worker_tree(store, worker)
    if not head or not tree:
        return None
    change = classify_change(before, GitSnapshot(head, tree))
    if change is CodeChange.NONE:
        return None

    targets = artifacts_invalidated_by(change)
    if not targets:
        # Same tree: behavioral evidence still holds, only lineage moves forward.
        for artifact in store.list_artifacts(job.id):
            if artifact.type in BEHAVIORAL_ARTIFACTS and not artifact.stale:
                store.save_artifact(relineage(artifact, head, tree))
        return Invalidation(change, head, 0)

    definition = find_workflow(worker.workflow)
    if definition is not None:
        targets = targets | definition.invalidates
    invalidated = 0
    for artifact in store.list_artifacts(job.id):
        if artifact.type in targets and not artifact.stale:
            artifact.stale = True
            artifact.stale_reason = f"{change.value} at {head[:8]}"
            store.save_artifact(artifact)
            invalidated += 1
    return Invalidation(change, head, invalidated)


def reconcile_job(store: Store, job: Job) -> list[Invalidation]:
    """Apply any durable, unfinished Git baselines before trusting run state."""
    outcomes = []
    for worker in store.list_workers(job.id):
        if worker.writable:
            outcome = apply_invalidation(store, worker, job)
            if outcome is not None:
                outcomes.append(outcome)
    return outcomes

