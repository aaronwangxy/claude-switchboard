# CLAUDE.md

Operating context for Claude Code sessions in this repository.

## What this is

Switchboard is a control plane over several long-running **native** Claude Code sessions. A
Python package under `src/switchboard`, driven by a Textual board.

It is deliberately thin. Claude Code owns the agent loop, tools, session persistence,
subagents, skills, settings inheritance, and the conversation UI. Switchboard owns
orchestration semantics: routing, the job/worker/worktree graph, workflow composition,
approval gates, attention, durable state, and contracts/evidence/freshness. **Prefer
deleting anything here that Claude already does well.**

`SessionManager` is the hub: it owns every invariant and is the only thing the UI and the
manager act through.

## Communication

- Think thoroughly, communicate briefly. Plain English. Concision must never cost reasoning
  quality.
- Prefer too little detail over noise; I will ask for more.
- Plans: roughly 10 short lines.
- Surface only decisions where human input genuinely matters. Do not restate context,
  completed work, or obvious next steps.

## Development philosophy

- This is a personal tool/prototype. Prefer simple, direct implementations over generalized
  infrastructure.
- Do not build for hypothetical requirements or unlikely edge cases.
- Complexity is justified by current behavior, normal usage, safety, recoverability, or
  preventing data loss / destructive Git operations / credential exposure.
- Prefer a working vertical slice over broad incomplete functionality.
- Preserve working behavior unless there is a concrete reason to change it.

## Where a behaviour belongs

| Question | Module |
| --- | --- |
| Which worktree *is* this job's change, and what did a change to it invalidate? | `core/lineage.py` |
| Is this change finished? | `core/evidence.py` |
| Should this composite step run? | `core/runs.py` |
| What does this workflow need, produce, and invalidate? | `workflows/spec.py` + the YAML |
| How is a native Claude process launched, observed, or entered? | `runtime/` |
| What may the Manager do? | `agents/manager_mcp.py` |
| Anything that mutates orchestration state | `core/session_manager.py` |

The UI holds no Git, SQLite, or worktree logic. Keep it that way.

Full module map and rationale: [`docs/architecture.md`](docs/architecture.md).

## Invariants that must not regress

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
10. A user or repository workflow may not redefine a built-in.
11. While the user is attached to a worker, Switchboard refuses to send to it.
12. Only a `MANAGED` turn that no human touched may harvest an artifact or advance a run.
13. Reserving a worker and sending it a prompt do not complete a composite step; only a
    successfully applied, manager-owned terminal event does.

Known gap: read-only workers keep `Bash`, so read-only is a tool policy and prompt
guarantee rather than a sandbox. See [`docs/troubleshooting.md`](docs/troubleshooting.md).

## Commands

`sb` is installed with `uv tool install --editable .`, so it runs *this* checkout: source
edits are live immediately, but a `pyproject.toml` dependency change needs
`uv tool install --editable . --reinstall`.

```bash
sb                                                             # launch (or: sb claude)
sb workflows                                                   # what routing can reach
sb config                                                      # effective config and paths
sb --log-file /tmp/switchboard.log                             # logs (otherwise discarded)
SB_BACKEND=scripted sb                                         # offline: no model calls

python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"   # dev tooling
./.venv/bin/python -m pytest -q                                # full suite, ~85s
./.venv/bin/ruff check src tests
./.venv/bin/mypy
./.venv/bin/python scripts/capture_ui.py                       # regenerate docs/ui-*.txt
```

Set `SB_HOME` to an isolated directory for any manual run that should not touch real state.

Tests come in four tiers and a test belongs to exactly one: `tests/unit` (deterministic),
`tests/integration` (scripted backend over real Git and SQLite), `tests/native` (real tmux
and a Claude-shaped fixture), `tests/ui` (Textual pilot). The native tier is not gated by an
environment variable and needs `tmux`. See [`docs/development.md`](docs/development.md).

## Git

- Clean, logical, atomic commits: one coherent, independently understandable change each. No
  giant catch-all commits, no meaningless micro-commits.
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
(subagents where useful) → test until criteria actually pass → deepest feasible end-to-end
smoke test or data-flow trace → fresh independent agent reviews the change, criteria, and
evidence → fix valid findings and rerun affected verification → concise change +
verification summary.

Use judgment: trivial questions and tiny mechanical changes skip this.

## Verification

- "Tests pass" is not sufficient evidence. Verify the observable behavior the request is
  about, as end-to-end as feasible.
- Verification must correspond to current HEAD. Rerun it after meaningful edits, rebases, or
  review fixes.
- Show a fix failing without its change wherever that is feasible.
- State clearly anything that could not be tested.

## Review comments

Classify each: valid / partially valid / invalid / already addressed / needs human input.
Fix valid ones; explain invalid ones rather than blindly implementing them; rerun affected
verification.

## Docs

`docs/` describes the finished system, not its history: architecture, runtime, workflows,
manager, configuration, development, troubleshooting. Keep it that way. Known limitations
belong in `docs/troubleshooting.md`; narrative and rationale belong in
`docs/project-evolution.md`; `docs/dogfood-report.md` is a dated field record and is not
edited to match new code.

## Maintaining this file

Keep it short enough to stay useful context: durable rules, invariants, and where things
live — not an architecture manual, not project history, not a second README. Remove stale
instructions when the architecture changes; because coding agents read this file, a stale
statement here is a correctness issue. Never include secrets, credentials, tokens, or
transcript contents.
