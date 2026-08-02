# CLAUDE.md

Operating context for Claude Code sessions in this repository.

## Communication

- Think thoroughly, communicate briefly. Plain English. Concision must never cost
  reasoning quality.
- Prefer too little detail over noise; I will ask for more.
- Plans: roughly 10 short lines.
- Surface only decisions where human input genuinely matters. Do not restate context,
  completed work, or obvious next steps.

## Development philosophy

- This is a personal tool/prototype. Prefer simple, direct implementations over
  generalized infrastructure.
- Do not build for hypothetical requirements or unlikely edge cases.
- Complexity is justified by current behavior, normal usage, safety, recoverability, or
  preventing data loss / destructive Git operations / credential exposure.
- Prefer a working vertical slice over broad incomplete functionality.
- Preserve working behavior unless there is a concrete reason to change it.

## Git

- Clean, logical, atomic commits: one coherent, independently understandable change each.
  No giant catch-all commits, no meaningless micro-commits.
- Plan the commit stack before substantial implementation.
- Leave `git status` clean.
- Never push, force-push, merge, discard my work, delete branches, or destructively remove
  worktrees unless I explicitly ask. Be especially conservative with dirty or unpushed
  worktrees.

## Context management

- Protect the primary context aggressively.
- Use bounded subagents for independent investigation, testing, review, or isolated
  implementation. Give each a narrow objective and only the relevant context. Never ask
  several agents to independently solve the same whole problem. Prefer fresh independent
  agents for review.
- Use worktrees where writable workstreams need isolation.
- Move durable conclusions into repository state/docs, not long chat history.

## Default ritual for meaningful changes

Understand → concise implementation-shape plan → identify genuine human decisions →
observable acceptance criteria → verification plan → intended commit stack → implement
(subagents where useful) → test until criteria actually pass → deepest feasible
end-to-end smoke test or data-flow trace → fresh independent agent reviews the change,
criteria, and evidence → fix valid findings and rerun affected verification → concise
change + verification summary.

Use judgment: trivial questions and tiny mechanical changes skip this.

## Verification

- "Tests pass" is not sufficient evidence. Verify the observable behavior the request is
  about, as end-to-end as feasible.
- Verification must correspond to current HEAD. Rerun it after meaningful edits, rebases,
  or review fixes.
- State clearly anything that could not be tested.

## Review comments

Classify each: valid / partially valid / invalid / already addressed / needs human input.
Fix valid ones; explain invalid ones rather than blindly implementing them; rerun
affected verification.

## Architecture

Python package `switchboard` under `src/`. `SessionManager` is the hub: it owns every
invariant and is the only thing the UI and the manager act through.

**What Switchboard owns, and what Claude owns.** Switchboard is a semantic control plane
over ongoing work, not a reimplementation of an agent. Claude Code owns the agent loop,
tools, session persistence and resume, subagents, skills, CLAUDE.md/settings inheritance,
permissions, and Dynamic Workflows *inside* a worker. Switchboard owns the layer above:
the manager and its routing, independent worker lifecycle, the
job/repo/branch/worktree/session graph, workflow selection and composition, approval
gates, attention routing, durable cross-session state, and contracts/evidence/freshness.
Prefer deleting anything in Switchboard that Claude already does well.

Dynamic Workflows deliberately do *not* back Switchboard's composite runs. Their
documented limits rule it out: no mid-run user input ("for sign-off between stages, run
each stage as its own workflow"), resume only within the same session, and their units
are subagents rather than sessions you can enter. Switchboard's runs are durable,
human-gated, and made of real worker sessions. They remain a good tool for a worker to
reach for.

| Module | Responsibility |
| --- | --- |
| `domain/` | Pydantic models, enums (incl. the allowed worker-transition table and attention priority), the three contracts, event kinds |
| `storage/` | SQLite schema (v3) and `Store` |
| `gitops/` | `runner` (argv-only git) and `WorktreeService` |
| `workflows/` | `WORKFLOWS` registry and deterministic artifact freshness |
| `agents/` | Native/scripted worker backends, persistent native manager, manager MCP, prompt composition |
| `runtime/` | Substrate-neutral durable runtime supervision and focused tmux process control |
| `routing/` | Deterministic router and attention-queue ordering |
| `core/` | `SessionManager` (the orchestrator) and guarded state transitions |
| `ui/` | Sparse session list, orchestration detail, and Manager input; presentation only |
| `app.py` | Bootstrap and the `sb` / `sb workflows` / `sb config` command surface |

**Manager / job / worker.** A *job* is one unit of work in a repository (usually a ticket)
with a stage and its artifacts. A *worker* is one independent Claude session with a role,
a cwd, a writable flag, and optionally a worktree; it may belong to a job. The *manager*
is a router, command palette, and status summarizer — never the system of record, and it
never writes code. `SessionManager` executes; the manager only proposes.

Two manager implementations share one `handle(text) -> str` contract: `DeterministicManager`
(rules only, for offline/scripted runs) and `PersistentNativeManager`. The production manager
is a generation-bound native Claude process on the same tmux substrate as workers. Its real
stdio MCP exposes only semantic orchestration operations through `SessionManager`; each call
revalidates the current manager identity, generation, kind, and ownership.

**Entry.** Manager and workers are persistent native Claude processes. Selecting a session
and pressing Enter (or `Ctrl+E`) attaches the terminal to its exact generation-bound tmux
target; entry never launches `claude --resume`, replaces the process, or interrupts an active
turn. Ownership becomes human, managed input is refused, and a composite attempt is tainted
and paused. Return requires explicit confirmation that Claude's composer is empty; the run
remains paused until `resume_run`. Same-server tmux clients switch directly, while clients on
another tmux server receive an actionable refusal instead of nesting.

**Worker backend.** `agents/backend.py` defines `WorkerSpec`/`WorkerHandle`/`WorkerEvent`
and the `WorkerBackend` protocol (start, send, stream, interrupt, stop, resume, health,
observe, adopt).
`NativeClaudeBackend` runs durable native Claude Code processes through the runtime
supervisor and tmux; `ScriptedWorkerBackend` emits the same normalized events in-process
for deterministic tests. Orchestration never touches tmux or hook payload types.

**Runtime instances.** Each worker has a durable, generation-numbered `RuntimeInstance`.
It records substrate-neutral process/turn state, manager-vs-human input ownership, Claude
session identity, launch fingerprint, opaque future substrate identity, and the Git baseline
for an active writable turn. Recovery observes the backend first: it adopts only an exact
runtime-id/generation match, reconstructs an absent manager-owned runtime as a new generation,
and refuses a live mismatch or an unobservable human-owned runtime.

The production native substrate uses one dedicated tmux
server, one session per runtime generation. `TmuxController` contains every tmux command and
parser; `TmuxRuntimeSupervisor` binds exact targets to `RuntimeInstance`. See
`docs/tmux-runtime.md` for topology, input, entry, and ownership rules.

**Session lifecycle.** `create_worker` → allocate a worktree if writable → `backend.start`
→ an asyncio pump task consumes `backend.stream` → `_apply` normalizes each event into
status changes, attention items, transcript rows, and artifact harvesting. `interrupt`
stops the turn and leaves the worker alive; `stop` is terminal. On startup `recover()` first
adopts an exact live generation. Lost manager-owned workers are reconstructed as a new native
generation from durable orchestration state; unsafe mismatches and unobservable human-owned
runtimes fail closed with a human-readable reason.

**Worktrees.** Only writable workers get one, always a fresh path under the managed root
(`<data dir>/worktrees/<repo>/<job>-<role>-<id8>`, branch `sb/<slug>-<id8>`) — never inside
the user's repository. Read-only workers get no worktree; they observe the job's writable
worktree path, or the repo root. `WorktreeService` owns creation, inspection, and the
cleanup decision; nothing else may remove a directory.

**Persistence.** `Store` is the system of record — not any transcript. Each table keeps
queryable columns plus the validated
model as JSON, so the domain models stay the single definition of shape. A model never
writes the database: workers emit a fenced ```json block and `extract_json_block` plus
Pydantic validation turn it into an artifact.

Attachment ownership and writable-turn Git baselines are durable runtime state. Git lineage
is reconciled on turn completion, detach, recovery, and before composite-run advancement;
an interrupt completion arriving after human handover may not consume that baseline.

**Status / events / attention.** Backend event → `_apply` → `_set_status` (guarded by
`assert_worker_transition`) → `raise_attention` → `emit` persists an `Event` and notifies
listeners → the UI repaints. The attention queue is ordered by `AttentionKind` ordinal then
age; snoozed workers drop out; a pin does not reorder but stops auto-advance from leaving.

**Manager context.** The native transcript is replaceable working memory. SQLite remains
long-term orchestration memory. A fresh manager reconstructs bounded objectives, jobs, runs,
workers, attention, decisions, contracts, and evidence through its MCP; worker transcripts
are not fed by default. Rotation stores only a bounded handoff for non-authoritative nuance.

**Runtime prompting.** `compose_worker_prompt` is an additive native Claude system-prompt
append layering concision, role, read-only, subagent, workflow, and verbosity policy.
`PROMPT_POLICY_VERSION` is persisted per worker.

**Workflows.** `WORKFLOWS` is loaded from YAML: built-in (`workflows/builtin/`), then the
user's (`~/.switchboard/workflows`), then each registered repository's
`.switchboard/workflows`. A malformed file is reported and skipped, never raised.
**Built-in names are reserved**: a workflow's `requires` and `mutates_code` are what
enforce contract prerequisites and worktree isolation, both default to permissive, and a
repository's workflows travel inside the repository they would be constraining. Each is a
`WorkflowDefinition` declaring allowed roles, required/produced artifacts, whether it
mutates code, what it invalidates, and a prompt template. `SessionManager` renders the
template, enforces prerequisites (`_assert_prerequisites`: implementation cannot run
without a current *and approved* implementation contract with no unanswered blocking
decisions), advances the job stage via `WORKFLOW_STAGE`, and harvests the artifact the
workflow produces. Freshness is decided from Git head/tree hashes alone — a same-tree
change only moves lineage forward, a tree change invalidates behavioral artifacts.

**Composite runs.** A composite workflow is a list of steps with a condition, an approval
gate, and a bounded `max_iterations`. `WorkflowRun` is persisted, so a run survives a
restart; `core/runs.py` evaluates every condition from stored state alone. The only
backwards move is a bounded repeat, so a run provably terminates. Safety invariants are
never configurable from a workflow. A durable completion marker, set only while applying
a trusted managed terminal event and its artifacts, authorizes advancement; assigning or
sending to a worker never does. Human intervention taints the current attempt and requires
explicit resume/replay before automatic mutation continues.

Each job persists one `authoritative_worktree_id`. The first writable worker establishes it,
and `set_authoritative_worktree` changes it explicitly. Reviewers, verifiers, freshness,
Git invalidation, and ready-to-push inspect only that lineage; other writable workers stay
isolated and cannot silently become the change under review.

**Mining.** `mine-workflows` is an ordinary read-only workflow whose input is
`SessionManager.workflow_history()` -- Switchboard's own record of what ran, in order,
per job. It produces `WORKFLOW_PROPOSALS`, which are inert; only `accept_proposal` writes
a proposal out, as an ordinary user workflow file.

**Claude settings inheritance.** Native workers use normal discovery in their repository,
including user, managed/company, project, and project-local configuration. The manager uses
the same configured wrapper/environment and native user/managed discovery from a dedicated
non-repository workspace, plus an additive prompt and generation-bound Switchboard MCP. Its
coding tools are disabled and workers never receive its MCP configuration.

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
    and `mutates_code` cannot be stripped by a file in the repository being worked on.
11. While the user is attached to a worker, Switchboard refuses to send to it.

Known gap: read-only workers keep Bash (reviewers and verifiers need it), so read-only is
enforced by tool policy and prompt, not a sandbox. See `docs/mvp-evidence.md` limitation 1.

## Extension points to preserve

- `WorkerBackend` protocol — keep native and deterministic backends behind the same
  orchestration boundary.
- `WORKFLOWS` registry — add a workflow by dropping YAML in `~/.switchboard/workflows`
  or a repository's `.switchboard/workflows`; no core change, no privileged built-in path.
- `ArtifactType` + `domain/contracts.py` — add an artifact type with its Pydantic schema.
- `routing/router.py` — routing rules stay deterministic and testable without a model.
- `AttentionKind` ordering — enum order *is* the priority.
- `storage/database.py` — bump `SCHEMA_VERSION` and extend `migrate()` on schema change.
- The UI holds no Git, SQLite, or worktree logic; keep it that way.

## Commands

`sb` is installed on my PATH with `uv tool install --editable .`, so it runs *this*
checkout: source edits are live immediately, but a `pyproject.toml` dependency change
needs `uv tool install --editable . --reinstall`.

```bash
sb                                                             # launch (or: sb claude)
sb --register /path/to/repo                                    # register a repo at startup
sb workflows                                                   # what routing can reach
sb config                                                      # effective config and paths
sb --log-file /tmp/switchboard.log                             # logs (otherwise discarded)
SB_BACKEND=scripted sb                                         # offline: no model calls

python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"   # dev tooling
./.venv/bin/python -m pytest -q                                # full suite, ~40s
./.venv/bin/ruff check src tests
./.venv/bin/mypy
./.venv/bin/python scripts/capture_ui.py                       # regenerate docs/ui-*.txt
```

`tests/unit` covers routing, attention, transitions, freshness, prompts, and worktree
safety; `tests/integration` drives the store, worktrees, manager tools, worker recovery,
the full feature loop, and the UI headlessly through Textual's pilot. Set `SB_HOME` to an
isolated directory for any manual run that should not touch real state.

## Maintenance

Keep this file short enough to stay useful context: durable rules and architecture, not
implementation trivia. Remove stale instructions when the architecture changes. Omit
anything obvious from the code that would not prevent a mistake. Never include secrets,
credentials, tokens, or transcript contents.
