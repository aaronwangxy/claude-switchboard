"""Worktree allocation and cleanup. The application owns this, never the agent."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from csm.config import worktree_root
from csm.domain.models import Job, Repository, Worker, Worktree
from csm.gitops import runner
from csm.gitops.runner import GitError, run_git

_SLUG = re.compile(r"[^a-zA-Z0-9._-]+")


class WorktreeSafetyError(RuntimeError):
    """A requested worktree operation would risk losing work."""


def slug(text: str, limit: int = 32) -> str:
    """Sanitise text for use as a single path component.

    Dots survive sanitisation (they are legal in names), so an all-dot result such as
    ``..`` must be rejected outright -- otherwise it would escape the managed root.
    """
    cleaned = _SLUG.sub("-", text).strip("-.").lower()
    return (cleaned or "job")[:limit]


@dataclass
class WorktreeState:
    worktree: Worktree
    exists: bool
    head: str | None = None
    branch: str | None = None
    dirty_files: list[str] = field(default_factory=list)
    unpushed_commits: list[str] = field(default_factory=list)

    @property
    def dirty(self) -> bool:
        return bool(self.dirty_files)


@dataclass
class CleanupDecision:
    safe: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def explanation(self) -> str:
        return "; ".join(self.reasons)


class WorktreeService:
    """Creates and removes isolated worktrees under an application-owned root.

    Never writes metadata into the user's source repository, and never removes a
    worktree that still holds uncommitted or unmerged work.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or worktree_root()).expanduser()

    # ------------------------------------------------------------------ paths

    def path_for(self, repository: Repository, job: Job | None, worker: Worker) -> Path:
        job_part = slug(job.external_ref or job.title) if job else "adhoc"
        worker_part = f"{slug(worker.role.value, 16)}-{str(worker.id)[:8]}"
        return self.root / slug(repository.name) / f"{job_part}-{worker_part}"

    def validate_path(self, path: Path) -> Path:
        """Refuse any path outside the application worktree root."""
        resolved = Path(path).expanduser().resolve()
        root = self.root.resolve()
        if resolved == root or root not in resolved.parents:
            raise WorktreeSafetyError(
                f"Refusing to operate on {resolved}: outside the managed worktree root {root}."
            )
        return resolved

    # ----------------------------------------------------------------- create

    def create_worktree(
        self, repository: Repository, job: Job | None, worker: Worker, base_ref: str
    ) -> Worktree:
        repo_path = repository.root_path
        if not runner.ref_exists(repo_path, base_ref):
            raise WorktreeSafetyError(
                f"Base ref {base_ref!r} does not exist in {repository.name}; refusing to branch."
            )
        path = self.path_for(repository, job, worker)
        self.validate_path(path)  # never create a worktree outside the managed root
        path.parent.mkdir(parents=True, exist_ok=True)
        branch = f"csm/{slug(job.external_ref or job.title) if job else 'adhoc'}-{str(worker.id)[:8]}"
        if path.exists():
            raise WorktreeSafetyError(f"Worktree path {path} already exists; refusing to reuse it.")
        run_git(repo_path, "worktree", "add", "-b", branch, str(path), base_ref)
        return Worktree(
            repository_id=repository.id,
            path=path,
            branch=branch,
            base_ref=base_ref,
            owner_worker_id=worker.id,
        )

    # ---------------------------------------------------------------- inspect

    def inspect_worktree(self, worktree: Worktree) -> WorktreeState:
        path = worktree.path
        if not path.exists() or not (path / ".git").exists():
            return WorktreeState(worktree=worktree, exists=False)
        try:
            return WorktreeState(
                worktree=worktree,
                exists=True,
                head=runner.head_commit(path),
                branch=runner.current_branch(path),
                dirty_files=runner.dirty_files(path),
                unpushed_commits=self.get_unpushed_commits(worktree),
            )
        except GitError:
            return WorktreeState(worktree=worktree, exists=False)

    def get_head(self, worktree: Worktree) -> str | None:
        try:
            return runner.head_commit(worktree.path)
        except GitError:
            return None

    def get_dirty_state(self, worktree: Worktree) -> list[str]:
        try:
            return runner.dirty_files(worktree.path)
        except GitError:
            return []

    def get_unpushed_commits(self, worktree: Worktree) -> list[str]:
        """Commits on the worktree branch that are not reachable from its base ref."""
        try:
            return runner.commits_between(worktree.path, worktree.base_ref, "HEAD")
        except GitError:
            return []

    # ---------------------------------------------------------------- cleanup

    def can_cleanup(self, worktree: Worktree, *, acknowledge_disposable: bool = False) -> CleanupDecision:
        state = self.inspect_worktree(worktree)
        if not state.exists:
            return CleanupDecision(safe=True, reasons=["Worktree directory is already gone."])
        reasons: list[str] = []
        if state.dirty:
            reasons.append(
                f"{len(state.dirty_files)} uncommitted change(s) in {worktree.path}: "
                + ", ".join(f.strip() for f in state.dirty_files[:5])
            )
        if state.unpushed_commits and not acknowledge_disposable:
            reasons.append(
                f"{len(state.unpushed_commits)} commit(s) on {worktree.branch} are not reachable "
                f"from {worktree.base_ref} and have not been acknowledged as disposable."
            )
        if reasons:
            return CleanupDecision(safe=False, reasons=reasons)
        return CleanupDecision(safe=True, reasons=["Clean worktree with no unmerged commits."])

    def cleanup_worktree(
        self, repository: Repository, worktree: Worktree, *, acknowledge_disposable: bool = False
    ) -> CleanupDecision:
        """Remove a worktree. Idempotent, and refuses when work would be lost.

        The branch itself is never deleted: branch deletion requires explicit user approval
        and is deliberately out of scope for automatic cleanup.
        """
        decision = self.can_cleanup(worktree, acknowledge_disposable=acknowledge_disposable)
        if not decision.safe:
            return decision
        path = self.validate_path(worktree.path) if worktree.path.exists() else worktree.path
        run_git(repository.root_path, "worktree", "remove", str(path), check=False)
        if path.exists():
            # `git worktree remove` can decline; fall back to removing the managed directory
            # only after validate_path proved it is inside our own root.
            self.validate_path(path)
            shutil.rmtree(path, ignore_errors=True)
        run_git(repository.root_path, "worktree", "prune", check=False)
        return CleanupDecision(safe=True, reasons=["Worktree removed; branch preserved."])

    # ------------------------------------------------------------- invariants

    @staticmethod
    def assert_single_writable_owner(
        worktree_id: UUID, existing_owner: UUID | None, candidate: UUID
    ) -> None:
        if existing_owner is not None and existing_owner != candidate:
            raise WorktreeSafetyError(
                f"Worktree {worktree_id} is already owned by writable worker {existing_owner}; "
                "a second writable worker cannot take it."
            )
