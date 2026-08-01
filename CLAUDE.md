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

Python package `csm` under `src/`. `SessionManager` is the hub: it owns every invariant and
is the only thing the UI and the manager act through.

| Module | Responsibility |
| --- | --- |
| `domain/` | Pydantic models, enums (incl. the allowed worker-transition table and attention priority), the three contracts, event kinds |
| `storage/` | SQLite schema (v1) and `Store` |
| `gitops/` | `runner` (argv-only git) and `WorktreeService` |
| `workflows/` | `WORKFLOWS` registry and deterministic artifact freshness |
| `agents/` | `WorkerBackend` protocol, SDK + scripted backends, manager, prompt composition, bounded snapshots |
| `routing/` | Deterministic router and attention-queue ordering |
| `core/` | `SessionManager` (the orchestrator) and guarded state transitions |
| `ui/` | Three-pane Textual app; presentation only |
| `app.py` | Bootstrap: builds every service and hands them to the UI |

**Manager / job / worker.** A *job* is one unit of work in a repository (usually a ticket)
with a stage and its artifacts. A *worker* is one independent Claude session with a role,
a cwd, a writable flag, and optionally a worktree; it may belong to a job. The *manager*
is a router, command palette, and status summarizer — never the system of record, and it
never writes code. `SessionManager` executes; the manager only proposes.

Two manager implementations share one `handle(text) -> str` contract: `DeterministicManager`
(rules only, no model) and `ModelManager` (a Claude session with ~15 constrained in-process
MCP tools, no Bash/Read/Edit, `max_turns=12`, falling back to the deterministic one on
error). The deterministic route is computed first and included in the snapshot; the model is
asked to follow it. Every tool handler re-validates against the domain, so a bad proposal
cannot do damage.

**Worker backend.** `agents/backend.py` defines `WorkerSpec`/`WorkerHandle`/`WorkerEvent`
and the `WorkerBackend` protocol (start, send, stream, interrupt, stop, resume, health).
`SdkWorkerBackend` runs one `ClaudeSDKClient` per worker; `ScriptedWorkerBackend` emits the
same normalized events in-process. Orchestration never touches SDK types.

**Session lifecycle.** `create_worker` → allocate a worktree if writable → `backend.start`
→ an asyncio pump task consumes `backend.stream` → `_apply` normalizes each event into
status changes, attention items, transcript rows, and artifact harvesting. `interrupt`
stops the turn and leaves the worker alive; `stop` is terminal. On startup `recover()`
resumes each worker by its stored `session_id`, or marks it `disconnected` with a
human-readable reason (missing worktree, no session id, resume failure).

**Worktrees.** Only writable workers get one, always a fresh path under the managed root
(`<data dir>/worktrees/<repo>/<job>-<role>-<id8>`, branch `csm/<slug>-<id8>`) — never inside
the user's repository. Read-only workers get no worktree; they observe the job's writable
worktree path, or the repo root. `WorktreeService` owns creation, inspection, and the
cleanup decision; nothing else may remove a directory.

**Persistence.** `Store` is the system of record — not any transcript. Each table keeps
queryable columns plus the validated
model as JSON, so the domain models stay the single definition of shape. A model never
writes the database: workers emit a fenced ```json block and `extract_json_block` plus
Pydantic validation turn it into an artifact.

**Status / events / attention.** Backend event → `_apply` → `_set_status` (guarded by
`assert_worker_transition`) → `raise_attention` → `emit` persists an `Event` and notifies
listeners → the UI repaints. The attention queue is ordered by `AttentionKind` ordinal then
age; snoozed workers drop out; a pin does not reorder but stops auto-advance from leaving.

**Manager context.** No growing transcript. Each turn builds a bounded snapshot (8
exchanges, 8 detailed workers with the rest summarized by status count, 10 events) from the
store, so the manager can be restarted at any time without losing operational state.

**Runtime prompting.** `compose_worker_prompt` *appends* to the SDK's `claude_code` preset —
never replaces it — layering concision policy, role policy, read-only note, subagent policy,
workflow policy, and verbosity. `PROMPT_POLICY_VERSION` is persisted per worker.

**Workflows.** 11 entries in `WORKFLOWS`, each a `WorkflowDefinition` declaring allowed
roles, required/produced artifacts, whether it mutates code, what it invalidates, and a
prompt template. `SessionManager` renders the template, enforces prerequisites
(`_assert_prerequisites`: implementation cannot run without a current *and approved*
implementation contract with no unanswered blocking decisions), advances the job stage via
`WORKFLOW_STAGE`, and harvests the artifact the workflow produces. Freshness is decided from
Git head/tree hashes alone — a same-tree change only moves lineage forward, a tree change
invalidates behavioral artifacts.

**Claude settings inheritance.** `config.setting_sources` (default `["user", "project"]`)
is passed to each worker SDK session, so workers pick up my user settings and the *target*
repository's `CLAUDE.md` and skills. The manager session uses `setting_sources=[]` and only
its MCP tools.

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

Known gap: read-only workers keep Bash (reviewers and verifiers need it), so read-only is
enforced by tool policy and prompt, not a sandbox. See `docs/mvp-evidence.md` limitation 1.

## Extension points to preserve

- `WorkerBackend` protocol — add a backend (e.g. a native `claude` CLI/PTY) without
  touching orchestration.
- `WORKFLOWS` registry — add a workflow declaratively; templates could become filesystem
  Skills without an API change.
- `ArtifactType` + `domain/contracts.py` — add an artifact type with its Pydantic schema.
- `routing/router.py` — routing rules stay deterministic and testable without a model.
- `AttentionKind` ordering — enum order *is* the priority.
- `storage/database.py` — bump `SCHEMA_VERSION` and extend `migrate()` on schema change.
- The UI holds no Git, SQLite, or worktree logic; keep it that way.

## Commands

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"   # install
./.venv/bin/python -m csm                                      # launch (or: ./.venv/bin/csm)
./.venv/bin/python -m csm --register /path/to/repo             # register a repo at startup
./.venv/bin/python -m csm --log-file /tmp/csm.log              # logs (otherwise discarded)
CSM_BACKEND=scripted ./.venv/bin/python -m csm                 # offline: no model calls

./.venv/bin/python -m pytest -q                                # full suite, ~40s
./.venv/bin/ruff check src tests
./.venv/bin/mypy
```

`tests/unit` covers routing, attention, transitions, freshness, prompts, and worktree
safety; `tests/integration` drives the store, worktrees, manager tools, worker recovery,
the full feature loop, and the UI headlessly through Textual's pilot. Set `CSM_HOME` to an
isolated directory for any manual run that should not touch real state.

## Maintenance

Keep this file short enough to stay useful context: durable rules and architecture, not
implementation trivia. Remove stale instructions when the architecture changes. Omit
anything obvious from the code that would not prevent a mistake. Never include secrets,
credentials, tokens, or transcript contents.
