# Architecture

Switchboard is a semantic control plane over ongoing work. It is deliberately thin,
because Claude Code already owns the hard part.

## What Switchboard owns, and what Claude owns

Claude Code owns the agent loop, tools, session persistence and resume, subagents, skills,
`CLAUDE.md` and settings inheritance, permission prompts, and its own conversation UI —
inside a worker. Switchboard owns the layer above:

- the manager and its routing;
- independent worker lifecycle;
- the job / repository / branch / worktree / session graph;
- workflow selection and composition;
- approval gates and attention routing;
- durable cross-session state;
- contracts, evidence, and freshness.

The posture is to keep deleting anything Switchboard does that Claude already does well.

Claude's Dynamic Workflows deliberately do **not** back Switchboard's composite runs.
Their documented limits rule it out: no mid-run user input ("for sign-off between stages,
run each stage as its own workflow"), resume only within the same session, and units that
are subagents rather than sessions you can step into. Switchboard's runs are durable,
human-gated, and made of real worker sessions. Dynamic Workflows remain a good tool for a
worker to reach for inside its own session.

## Manager, job, worker

A **job** is one unit of work in a repository — usually a ticket — with a stage and its
artifacts. A **worker** is one independent Claude session with a role, a working
directory, a writable flag, and optionally a worktree; it may belong to a job. The
**manager** is a router, a command palette, and a status summariser. It is never the
system of record and it never writes code: `SessionManager` executes, the manager only
proposes.

```
Switchboard
├── SessionManager + domain state   the only thing that changes anything
├── manager Claude (native)         constrained, manager-only Switchboard MCP
├── worker Claudes (native)         no orchestration authority at all
├── workflow / composite engine     atomic prompts and durable multi-step runs
├── contracts, evidence, approvals  what decides whether a change is finished
├── Git worktree lineage            one authoritative tree per job
└── durable runtime + tmux          processes that outlive the controller
```

## Modules

| Module | Responsibility |
| --- | --- |
| `domain/` | Pydantic models, enums (the worker-transition table, attention priority), the three contracts, event kinds |
| `storage/` | SQLite schema and `Store`, the system of record |
| `gitops/` | `runner` (argv-only git) and `WorktreeService` |
| `workflows/` | The `WORKFLOWS` registry, YAML loading, and deterministic artifact freshness |
| `agents/` | Worker backends, the persistent native manager, the manager MCP, prompt composition |
| `runtime/` | Substrate-neutral runtime supervision, the tmux controller, the Claude hook bridge |
| `routing/` | The deterministic router and attention-queue ordering |
| `core/` | `SessionManager`, the composite-run conditions, Git lineage, the evidence gate, guarded transitions |
| `ui/` | The session list, orchestration detail, and Manager input — presentation only |
| `app.py` | Bootstrap and the `sb` / `sb workflows` / `sb config` command surface |

Where a behaviour belongs:

- *"which worktree is this job's change, and what did a change to it invalidate?"* →
  `core/lineage.py`
- *"is this change finished?"* → `core/evidence.py`
- *"should this composite step run?"* → `core/runs.py`
- *"what does this workflow need and produce?"* → `workflows/spec.py` and the YAML
- everything that mutates orchestration state → `core/session_manager.py`

## Session lifecycle

```
create_worker
  → allocate a worktree if writable
  → backend.start
  → an asyncio pump consumes backend.stream
  → _apply normalises each event into status changes, attention items,
    transcript rows, and harvested artifacts
```

`interrupt` stops the current turn and leaves the worker alive. `stop` is terminal. On
startup, `recover()` observes the backend first: it adopts only an exact
runtime-id/generation match, reconstructs an absent manager-owned runtime as a new
generation, and refuses a live mismatch or an unobservable human-owned runtime.

## Status, events, attention

```
backend event → _apply → _set_status (guarded by assert_worker_transition)
              → raise_attention → emit persists an Event and notifies listeners
              → the UI repaints
```

The attention queue is ordered by `AttentionKind` ordinal, then by age. Snoozed workers
drop out. A pin does not reorder anything; it stops auto-advance from leaving.

## Persistence

`Store` is the system of record — not any transcript. Each table keeps its queryable
columns plus the validated model as JSON, so the domain models stay the single definition
of shape. **A model never writes the database**: workers emit a fenced ```json block, and
`extract_json_block` plus Pydantic validation turn it into an artifact.

Attachment ownership and writable-turn Git baselines are durable runtime state. Git
lineage is reconciled on turn completion, on detach, on recovery, and before a composite
run advances; an interrupt completion that arrives after a human took over may not consume
that baseline.

## Worktrees

Only writable workers get a worktree, always at a fresh path under the managed root
(`<data dir>/worktrees/<repo>/<job>-<role>-<id8>`, on branch `sb/<slug>-<id8>`) — never
inside the user's repository. Read-only workers get none: they observe the job's
authoritative worktree, or the repository root. `WorktreeService` owns creation,
inspection, and the cleanup decision; nothing else may remove a directory.

Each job persists one `authoritative_worktree_id`. The first writable worker establishes
it and `set_authoritative_worktree` changes it explicitly. Reviewers, verifiers, freshness,
Git invalidation, and the ready-to-push gate inspect only that lineage.

## Safety invariants

Enforced in ordinary Python, never by asking a model to behave:

1. Git is invoked as an argument array; input never reaches a shell.
2. No worktree is created or removed outside the managed root.
3. One writable owner per worktree; each writable worker gets a distinct path.
4. Cleanup requires explicit confirmation and refuses to discard uncommitted or unmerged
   work. Branches are never deleted; nothing pushes, force-pushes, or merges.
5. Destructive requests are gated by the router *before* the model is consulted.
6. Worker status changes must satisfy `ALLOWED_WORKER_TRANSITIONS`.
7. Workflow prerequisites and `ready_to_push` are computed from stored state, not judgment.
8. Workers run with `mcp_servers={}`, so manager tools are structurally unreachable.
9. A malformed manager tool call returns a refusal, never an exception that kills the turn.
10. A user or repository workflow may not redefine a built-in, so declared prerequisites
    and `mutates_code` cannot be stripped by a file inside the repository being worked on.
11. While the user is attached to a worker, Switchboard refuses to send to it.

Read-only workers keep `Bash`, because reviewers and verifiers need it, so read-only is a
tool-policy and prompt guarantee rather than a sandbox. See
[troubleshooting.md](troubleshooting.md#known-limitations).

## Extension points to preserve

- `WorkerBackend` — keep the native and deterministic backends behind one orchestration
  boundary.
- `WORKFLOWS` — add a workflow by dropping YAML in `~/.switchboard/workflows` or a
  repository's `.switchboard/workflows`; no core change, no privileged built-in path.
- `ArtifactType` + `domain/contracts.py` — add an artifact type with its Pydantic schema.
- `routing/router.py` — routing rules stay deterministic and testable without a model.
- `AttentionKind` ordering — the enum order *is* the priority.
- `storage/database.py` — bump `SCHEMA_VERSION` and extend `migrate()` on a schema change.
- The UI holds no Git, SQLite, or worktree logic. Keep it that way.
