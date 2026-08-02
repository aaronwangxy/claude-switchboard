"""Attention priority ordering and auto-advance selection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from switchboard.domain.enums import AttentionKind, WorkerRole, WorkerStatus
from switchboard.domain.models import AttentionItem, Worker
from switchboard.routing.attention import is_snoozed, next_actionable, prioritize

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def make_worker(title: str, **kwargs) -> Worker:
    return Worker(
        title=title,
        role=WorkerRole.IMPLEMENTER,
        status=WorkerStatus.BLOCKED,
        repository_id=__import__("uuid").uuid4(),
        cwd=Path("/tmp/repo"),
        **kwargs,
    )


def item(worker: Worker, kind: AttentionKind, minutes: int = 0) -> AttentionItem:
    return AttentionItem(
        worker_id=worker.id,
        kind=kind,
        reason=kind.value,
        created_at=NOW + timedelta(minutes=minutes),
    )


def test_priority_follows_the_specified_order():
    workers = {}
    items = []
    # Deliberately created in reverse priority order and reverse time order.
    for offset, kind in enumerate(reversed(list(AttentionKind))):
        worker = make_worker(kind.value)
        workers[worker.id] = worker
        items.append(item(worker, kind, minutes=offset))
    ordered = prioritize(items, workers)
    assert [i.kind for i in ordered] == list(AttentionKind)


def test_equal_priority_breaks_ties_by_age():
    a, b = make_worker("a"), make_worker("b")
    workers = {a.id: a, b.id: b}
    older = item(a, AttentionKind.HUMAN_DECISION, minutes=0)
    newer = item(b, AttentionKind.HUMAN_DECISION, minutes=5)
    assert prioritize([newer, older], workers) == [older, newer]


def test_handled_items_leave_the_queue():
    worker = make_worker("a")
    entry = item(worker, AttentionKind.HUMAN_DECISION)
    entry.handled = True
    assert prioritize([entry], {worker.id: worker}) == []


def test_snoozed_workers_are_hidden_until_their_snooze_expires():
    worker = make_worker("a", snoozed_until=NOW + timedelta(minutes=30))
    entry = item(worker, AttentionKind.HUMAN_DECISION)
    assert is_snoozed(worker, NOW)
    assert prioritize([entry], {worker.id: worker}, at=NOW) == []
    assert prioritize([entry], {worker.id: worker}, at=NOW + timedelta(hours=1)) == [entry]


# --------------------------------------------------------------- auto-advance


@pytest.fixture
def queue():
    blocked = make_worker("blocked")
    ready = make_worker("ready")
    workers = {blocked.id: blocked, ready.id: ready}
    items = [
        item(ready, AttentionKind.WORK_COMPLETE, minutes=1),
        item(blocked, AttentionKind.HUMAN_DECISION, minutes=2),
    ]
    return blocked, ready, workers, items


def test_auto_advance_opens_the_highest_priority_other_worker(queue):
    blocked, ready, workers, items = queue
    assert (
        next_actionable(
            items, workers, current_worker_id=ready.id, auto_advance=True, user_is_typing=False
        )
        == blocked.id
    )


def test_auto_advance_never_switches_while_the_user_is_typing(queue):
    blocked, ready, workers, items = queue
    assert (
        next_actionable(
            items, workers, current_worker_id=ready.id, auto_advance=True, user_is_typing=True
        )
        is None
    )


def test_auto_advance_can_be_paused(queue):
    blocked, ready, workers, items = queue
    assert (
        next_actionable(
            items, workers, current_worker_id=ready.id, auto_advance=False, user_is_typing=False
        )
        is None
    )


def test_a_pinned_current_worker_holds_the_pane(queue):
    blocked, ready, workers, items = queue
    ready.pinned = True
    assert (
        next_actionable(
            items, workers, current_worker_id=ready.id, auto_advance=True, user_is_typing=False
        )
        is None
    )


def test_nothing_actionable_returns_none_so_focus_falls_back_to_the_manager(queue):
    blocked, ready, workers, _ = queue
    assert (
        next_actionable(
            [], workers, current_worker_id=ready.id, auto_advance=True, user_is_typing=False
        )
        is None
    )


def test_auto_advance_skips_the_worker_already_open(queue):
    blocked, ready, workers, items = queue
    # The blocked worker is already open and is the top item; the next one is `ready`.
    assert (
        next_actionable(
            items, workers, current_worker_id=blocked.id, auto_advance=True, user_is_typing=False
        )
        == ready.id
    )
