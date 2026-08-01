"""Artifact freshness: Git facts, not model judgment, decide what is stale."""

from __future__ import annotations

from uuid import uuid4

import pytest

from csm.domain.enums import ArtifactType
from csm.domain.models import Artifact
from csm.workflows.freshness import (
    BEHAVIORAL_ARTIFACTS,
    CodeChange,
    GitSnapshot,
    artifacts_invalidated_by,
    classify_change,
    is_fresh,
    relineage,
)

A = GitSnapshot(head="aaa", tree="t1")


def test_no_change_at_all():
    assert classify_change(A, GitSnapshot("aaa", "t1")) is CodeChange.NONE


def test_new_commit_with_a_new_tree_is_an_implementation_edit():
    assert classify_change(A, GitSnapshot("bbb", "t2")) is CodeChange.IMPLEMENTATION_EDIT


def test_new_commit_with_the_same_tree_is_a_pure_restack():
    assert classify_change(A, GitSnapshot("bbb", "t1")) is CodeChange.PURE_RESTACK


def test_conflicted_rebase_is_classified_separately():
    assert (
        classify_change(A, GitSnapshot("bbb", "t2"), had_conflicts=True)
        is CodeChange.REBASE_WITH_CONFLICTS
    )


@pytest.mark.parametrize(
    "change",
    [CodeChange.IMPLEMENTATION_EDIT, CodeChange.REVIEW_COMMENTS, CodeChange.REBASE_WITH_CONFLICTS],
)
def test_content_changes_invalidate_verification_and_review(change):
    assert artifacts_invalidated_by(change) == BEHAVIORAL_ARTIFACTS


def test_a_clean_rebase_invalidates_verification_and_flags_review_for_refresh():
    invalidated = artifacts_invalidated_by(CodeChange.CLEAN_REBASE)
    assert ArtifactType.SMOKE_VERIFICATION in invalidated
    assert ArtifactType.REVIEW in invalidated


@pytest.mark.parametrize("change", [CodeChange.COMMIT_MESSAGE_ONLY, CodeChange.PURE_RESTACK])
def test_tree_preserving_changes_invalidate_nothing(change):
    assert artifacts_invalidated_by(change) == frozenset()


def make_artifact(type_: ArtifactType, **kwargs) -> Artifact:
    return Artifact(job_id=uuid4(), type=type_, **kwargs)


def test_behavioural_artifacts_are_stale_when_the_tree_moved():
    artifact = make_artifact(ArtifactType.VERIFICATION, head_commit="aaa", tree_hash="t1")
    assert is_fresh(artifact, "aaa", "t1")
    assert not is_fresh(artifact, "bbb", "t2")


def test_a_behavioural_artifact_survives_a_same_tree_rewrite():
    artifact = make_artifact(ArtifactType.REVIEW, head_commit="aaa", tree_hash="t1")
    assert is_fresh(artifact, "bbb", "t1"), "same tree means the evidence still holds"


def test_an_explicitly_stale_artifact_is_never_fresh():
    artifact = make_artifact(
        ArtifactType.VERIFICATION, head_commit="aaa", tree_hash="t1", stale=True
    )
    assert not is_fresh(artifact, "aaa", "t1")


def test_contracts_are_not_invalidated_by_head_movement():
    artifact = make_artifact(ArtifactType.IMPLEMENTATION_CONTRACT, head_commit="aaa")
    assert is_fresh(artifact, "zzz", "t9")


def test_relineage_moves_lineage_without_marking_stale():
    artifact = make_artifact(ArtifactType.VERIFICATION, head_commit="aaa", tree_hash="t1")
    relineage(artifact, "bbb", "t1")
    assert artifact.head_commit == "bbb"
    assert artifact.stale is False
