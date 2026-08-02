"""Attention queue ordering and auto-advance selection."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from switchboard.domain.enums import ATTENTION_PRIORITY
from switchboard.domain.models import AttentionItem, Worker


def is_snoozed(worker: Worker, at: datetime | None = None) -> bool:
    if worker.snoozed_until is None:
        return False
    return worker.snoozed_until > (at or datetime.now(UTC))


def sort_key(item: AttentionItem) -> tuple[int, datetime]:
    return (ATTENTION_PRIORITY[item.kind], item.created_at)


def prioritize(
    items: list[AttentionItem], workers: dict[UUID, Worker], at: datetime | None = None
) -> list[AttentionItem]:
    """Order actionable items by `AttentionKind` priority, dropping snoozed workers.

    Pins do not change ordering; they only stop auto-advance from moving away.
    """
    live = [
        item
        for item in items
        if not item.handled
        and item.worker_id in workers
        and not is_snoozed(workers[item.worker_id], at)
    ]
    return sorted(live, key=sort_key)


def next_actionable(
    items: list[AttentionItem],
    workers: dict[UUID, Worker],
    *,
    current_worker_id: UUID | None,
    auto_advance: bool,
    user_is_typing: bool,
    at: datetime | None = None,
) -> UUID | None:
    """Which worker should open next, or None to stay put / fall back to the manager.

    Returns None when auto-advance is off, the user is mid-message, the current worker
    is pinned, or nothing needs attention.
    """
    if not auto_advance or user_is_typing:
        return None
    current = workers.get(current_worker_id) if current_worker_id else None
    if current is not None and current.pinned:
        return None
    ordered = prioritize(items, workers, at)
    for item in ordered:
        if item.worker_id != current_worker_id:
            return item.worker_id
    return None


