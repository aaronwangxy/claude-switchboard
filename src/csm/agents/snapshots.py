"""Bounded manager state snapshots.

The manager turn is mostly stateless: durable facts live in SQLite and each turn gets a
compact snapshot rather than a growing transcript. The manager can be restarted at any
time without losing operational state.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from uuid import UUID

from csm.domain.enums import JobStage, WorkerStatus
from csm.domain.models import AttentionItem, Event, Job, Repository, Worker
from csm.routing.router import RouteProposal
from csm.workflows.registry import WORKFLOWS

MAX_EXCHANGES = 8
MAX_WORKERS_IN_DETAIL = 8
MAX_EVENTS = 10

ACTIVE_STATUSES = frozenset(
    {WorkerStatus.STARTING, WorkerStatus.WORKING, WorkerStatus.BLOCKED, WorkerStatus.IDLE}
)


@dataclass
class Exchange:
    user: str
    manager: str


@dataclass
class SnapshotInput:
    repositories: list[Repository]
    jobs: list[Job]
    workers: list[Worker]
    attention: list[AttentionItem]
    events: list[Event]
    exchanges: list[Exchange] = field(default_factory=list)
    selected_worker_id: UUID | None = None
    selected_job_id: UUID | None = None
    referenced_job_ids: set[UUID] = field(default_factory=set)


def _worker_line(worker: Worker, job: Job | None) -> str:
    bits = [
        f"id={worker.id}",
        f"title={worker.title!r}",
        f"role={worker.role.value}",
        f"status={worker.status.value}",
        f"writable={worker.writable}",
    ]
    if job is not None:
        bits.append(f"job={job.external_ref or job.title!r}")
    if worker.waiting_for:
        bits.append(f"waiting_for={worker.waiting_for[:80]!r}")
    if worker.pinned:
        bits.append("pinned")
    if worker.active_helpers:
        bits.append(f"helpers={worker.active_helpers}")
    return "- " + ", ".join(bits)


def build_snapshot(data: SnapshotInput, route: RouteProposal | None = None) -> str:
    """Render the bounded snapshot. History is never loaded indiscriminately."""
    jobs_by_id = {job.id: job for job in data.jobs}
    lines: list[str] = []

    lines.append("## Registered repositories")
    lines += [f"- id={r.id}, name={r.name}, path={r.root_path}" for r in data.repositories] or [
        "- (none registered)"
    ]

    # Completed and failed jobs are excluded unless this turn explicitly referenced them.
    open_jobs = [
        j
        for j in data.jobs
        if j.stage not in (JobStage.COMPLETED, JobStage.FAILED) or j.id in data.referenced_job_ids
    ]
    lines.append("\n## Open jobs")
    lines += [
        f"- id={j.id}, ref={j.external_ref or '-'}, title={j.title!r}, stage={j.stage.value}, "
        f"base={j.base_ref}"
        for j in open_jobs
    ] or ["- (none)"]

    active = [w for w in data.workers if w.status in ACTIVE_STATUSES]
    shown = active[:MAX_WORKERS_IN_DETAIL]
    lines.append("\n## Workers")
    lines += [
        _worker_line(w, jobs_by_id.get(w.job_id) if w.job_id else None) for w in shown
    ] or ["- (none)"]
    # Everything not shown in detail -- overflow plus every inactive worker -- is
    # summarised by status count so the snapshot stays bounded.
    remainder = active[MAX_WORKERS_IN_DETAIL:] + [
        w for w in data.workers if w.status not in ACTIVE_STATUSES
    ]
    if remainder:
        counts = Counter(w.status.value for w in remainder)
        lines.append(
            "- plus " + ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
        )

    lines.append("\n## Attention queue (highest priority first)")
    lines += [
        f"- worker={i.worker_id}, kind={i.kind.value}, reason={i.reason[:120]!r}"
        for i in data.attention
    ] or ["- (empty)"]

    lines.append("\n## Recent events")
    lines += [
        f"- {e.kind}: {e.summary[:100]}" for e in data.events[:MAX_EVENTS]
    ] or ["- (none)"]

    lines.append("\n## Available workflows")
    lines.append("- " + ", ".join(sorted(WORKFLOWS)))

    lines.append("\n## Selection")
    lines.append(f"- selected_worker_id={data.selected_worker_id}")
    lines.append(f"- selected_job_id={data.selected_job_id}")

    if data.exchanges:
        lines.append("\n## Recent manager exchanges")
        for exchange in data.exchanges[-MAX_EXCHANGES:]:
            lines.append(f"- user: {exchange.user[:200]}")
            lines.append(f"  manager: {exchange.manager[:200]}")

    if route is not None:
        lines.append("\n## Deterministic route proposal")
        lines.append(f"- {route.describe()}")
        if route.workflow:
            lines.append(f"- workflow={route.workflow}")
        if route.worker_id:
            lines.append(f"- worker_id={route.worker_id}")
        if route.job_id:
            lines.append(f"- job_id={route.job_id}")
        if route.repository_id:
            lines.append(f"- repository_id={route.repository_id}")

    return "\n".join(lines)
