"""Deterministic artifact freshness and invalidation.

Freshness is decided from Git commit and tree information, never from model judgment.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from csm.domain.enums import ArtifactType
from csm.domain.models import Artifact

BEHAVIORAL_ARTIFACTS = frozenset(
    {ArtifactType.VERIFICATION, ArtifactType.SMOKE_VERIFICATION, ArtifactType.REVIEW}
)


class CodeChange(str, Enum):
    """How the worktree changed, as observed from Git.

    Only NONE, PURE_RESTACK, and IMPLEMENTATION_EDIT are reachable from production
    today: classify_change() distinguishes changes by head/tree hash alone, and no
    caller passes had_conflicts=True. The remaining members are classified by
    artifacts_invalidated_by() and covered by unit tests, but nothing currently
    produces them -- a rebase that changes the tree is reported as an
    IMPLEMENTATION_EDIT, which invalidates a superset, the conservative direction.
    See docs/mvp-evidence.md limitation 5.
    """

    IMPLEMENTATION_EDIT = "implementation_edit"
    REVIEW_COMMENTS = "review_comments"
    REBASE_WITH_CONFLICTS = "rebase_with_conflicts"
    CLEAN_REBASE = "clean_rebase"
    COMMIT_MESSAGE_ONLY = "commit_message_only"
    PURE_RESTACK = "pure_restack"
    NONE = "none"


@dataclass(frozen=True)
class GitSnapshot:
    head: str
    tree: str


def classify_change(before: GitSnapshot, after: GitSnapshot, *, had_conflicts: bool = False) -> CodeChange:
    """Classify a change from head/tree hashes alone.

    A same-tree, different-head change is history rewriting (restack or reword); a
    different tree is a real content change.
    """
    if before.head == after.head and before.tree == after.tree:
        return CodeChange.NONE
    if before.tree == after.tree:
        return CodeChange.PURE_RESTACK
    if had_conflicts:
        return CodeChange.REBASE_WITH_CONFLICTS
    return CodeChange.IMPLEMENTATION_EDIT


def artifacts_invalidated_by(change: CodeChange) -> frozenset[ArtifactType]:
    """Which artifact types a given change makes stale."""
    match change:
        case CodeChange.IMPLEMENTATION_EDIT | CodeChange.REVIEW_COMMENTS | CodeChange.REBASE_WITH_CONFLICTS:
            return BEHAVIORAL_ARTIFACTS
        case CodeChange.CLEAN_REBASE:
            # At minimum smoke/integration verification is stale; review is marked for
            # policy-based refresh, which we model as stale too so it must be rerun.
            return frozenset(
                {ArtifactType.SMOKE_VERIFICATION, ArtifactType.VERIFICATION, ArtifactType.REVIEW}
            )
        case CodeChange.COMMIT_MESSAGE_ONLY | CodeChange.PURE_RESTACK:
            # The tree is unchanged, so behavioral evidence still holds; only lineage moves.
            return frozenset()
        case _:
            return frozenset()


def is_fresh(artifact: Artifact, current_head: str, current_tree: str | None = None) -> bool:
    """An artifact is fresh when it is not marked stale and matches the current tree/head."""
    if artifact.stale:
        return False
    if artifact.type in BEHAVIORAL_ARTIFACTS:
        if current_tree and artifact.tree_hash:
            return artifact.tree_hash == current_tree
        return artifact.head_commit == current_head
    return True


def relineage(artifact: Artifact, head: str, tree: str | None) -> Artifact:
    """Move an artifact's lineage forward without invalidating it (same tree, new head)."""
    artifact.head_commit = head
    if tree:
        artifact.tree_hash = tree
    return artifact
