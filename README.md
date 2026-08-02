# Switchboard

A one-window control plane for multiple independent Claude coding sessions.

## The problem

Running multiple coding agents is useful. Managing them manually is tedious:

- Open another terminal.
- Find or create the correct Git worktree.
- Start Claude in the correct directory.
- Remember which terminal belongs to which task.
- Check every session to see which one is blocked or complete.
- Prevent two sessions from modifying the same files.
- Clean up processes, branches, and worktrees safely.

The engineering work can run in parallel, but the human becomes a process manager.

Switchboard removes that operational burden while keeping every worker
isolated and directly interactive.

Paste a ticket into the Manager input and it is routed and consumed
by the right agent — planned, implemented, verified, reviewed — surfacing to you only
where a human decision genuinely matters.

Switchboard is deliberately thin. Claude Code already owns the agent loop, tools,
session persistence, subagents, skills, and settings inheritance, and Switchboard reuses
all of it. What Switchboard owns is the layer above: understanding a casual reference to
work in flight, resolving it against the durable relationships between jobs,
repositories, branches, worktrees, and sessions, choosing the development ritual you
prefer, and holding the contracts and evidence that decide whether a change is actually
finished.

## Contracts

What makes delegation at scale reliable is not a better prompt; it is an executable
contract around the agent. Across Anthropic, OpenAI, and Cognition, a similar pattern is
emerging: agree on the intended implementation shape, define observable success criteria,
and require independent evidence that those criteria hold.

I think of these as the **implementation contract**, **behavior contract**, and
**evidence contract**.

| Contract | Question | Produced by |
| --- | --- | --- |
| Implementation | What shape should the solution take? | planner |
| Behavior | What must observably work? | planner |
| Evidence | What proof demonstrates each behavior? | verifier |

They are stored as structured artifacts, not prose in a transcript, and the application
enforces them: implementation cannot start without an approved plan, a code change
deterministically invalidates the verification and review that no longer apply, and
"ready to push" is computed from stored evidence rather than asserted by a model.

## How it works

Paste a ticket, ask a question, or say "rebase this" into one manager input. The manager
routes it: to an existing worker, to a reusable workflow, or to a new independent worker
in its own Git worktree.

```
you  →  manager Claude  →  resolve the job/repo/change  →  select a workflow
                                                            ↓
                                    reuse or launch an independent Claude worker
```

Manager and every worker are persistent native Claude Code sessions. Workers inherit normal
user, managed/company, project, and project-local configuration in their repository or
worktree. Manager uses the configured executable and native user/managed configuration from
an isolated non-repository workspace, with only Switchboard's manager MCP. Highlight any
session and press Enter (or `Ctrl+E`) to enter that exact live process. No resume or replacement
process is involved.

```bash
tmux -S <switchboard socket> attach-session -t <runtime session>
```

Switchboard does not interrupt an active turn when you enter. It refuses to send while you are
there and pauses any workflow run the worker belongs to. Clear any unsubmitted composer text
before handing control back. The run stays paused until you say "resume the run"—you may have
edited by hand, so whether the ritual should carry on is yours to decide.

```
+------------------------------+----------------------------------------+
| Manager status + goal input  | Selected session                       |
|                              |  workflow / lifecycle / ownership       |
+------------------------------+  dependencies / worktree / evidence    |
| Sessions + attention queue   |  Enter → exact native Claude           |
+------------------------------+----------------------------------------+
```

## Install

Requires `git` and the `claude` CLI on your `PATH`; [uv](https://docs.astral.sh/uv/)
supplies the Python (3.12+).

On a machine without a checkout:

```bash
uv tool install git+https://github.com/aaronwangxy/claude-switchboard
```

That is the whole install — `sb` lands on your `PATH` with the built-in workflows inside
the package. Re-run with `--reinstall` to pick up a newer `main`.

From a checkout, to work on Switchboard itself:

```bash
uv tool install --editable .    # puts `sb` on your PATH
```

`--editable` points the installed command at this checkout, so an edit here is live on
the next `sb` — which is what you want while iterating. Drop it for a frozen copy. Either
way, re-run with `--reinstall` after changing dependencies in `pyproject.toml`.

To try it without installing anything:

```bash
uvx --from . sb
```

## Run

```bash
sb                          # launch the interface (or: sb claude)
sb --register /path/repo    # register a repository at startup
sb workflows                # what this installation can route to
sb config                   # effective configuration and its paths
SB_BACKEND=scripted sb      # offline demo: no model calls
```

Press `?` in the app for the key bindings. Claude owns conversation rendering; Switchboard
shows durable orchestration state around the sessions.

## Workflows

A workflow is routing metadata plus a prompt: what it needs, what it produces, what it
invalidates, and whether it needs a fresh Claude. They are YAML, and adding one requires
no change to Switchboard:

```yaml
name: post-rebase-verify
description: Re-verify a change after rebasing it, because the old evidence no longer holds.
steps:
  - workflow: rebase-stack
  - workflow: full-verify
  - workflow: smoke-test
```

Drop that in `~/.switchboard/workflows/` for yourself, or in a repository's
`.switchboard/workflows/` so the convention travels with the clone. Built-in names are
reserved — a file in a repository must not be able to strip the prerequisites that keep
implementation from running without an approved plan. `sb workflows` lists everything
loaded. `mine-workflows` reads Switchboard's own record of what you have been running and
*proposes* workflows for rituals you keep assembling by hand; a proposal changes nothing
until you accept it.

## Develop

The tooling runs from a plain virtualenv, independent of the installed `sb`:

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/python -m pytest -q
./.venv/bin/ruff check src tests
./.venv/bin/mypy
```

## Configuration

| Path / variable | Purpose |
| --- | --- |
| `~/.config/switchboard/config.yaml` | preferences and model policy (see `config.example.yaml`) |
| `~/.local/share/switchboard/switchboard.db` | durable state |
| `~/.local/share/switchboard/worktrees/` | managed worktrees, never inside your repo |
| `SB_HOME` | relocate the data directory |
| `SB_CONFIG` | alternate config file |
| `SB_STRONG_MODEL` / `SB_FAST_MODEL` | default models per role |
| `SB_BACKEND=scripted` | use the deterministic in-process backend |
| `SB_WORKFLOWS_DIR` | relocate the user workflow directory |
| `claude.executable` | launch a wrapper such as `company-claude` instead of `claude` |
| `worktree_bootstrap.files` | gitignored files to copy into a new worktree (empty by default) |

## Safety

The application, not the agent, owns every destructive path. Worktrees live outside your
repository and only under the managed root; cleanup requires explicit confirmation and
refuses to remove a worktree holding uncommitted or unmerged work; branches are never
deleted; nothing pushes, force-pushes, or merges.

## Status

Working prototype, built for personal use. Architecture and conventions are in
[`CLAUDE.md`](CLAUDE.md). In [`docs/`](docs/): the original specification, the MVP
verification record, and [`harness-evidence.md`](docs/harness-evidence.md) — what was
verified for the workflow-harness milestone, including why native Dynamic Workflows do
not back Switchboard's composite runs, and what was not verified.
