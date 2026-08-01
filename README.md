# Claude Session Manager

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

Claude Session Manager removes that operational burden while keeping every worker
isolated and directly interactive.

Where it is going: paste a ticket into the manager chat and it gets routed and consumed
by the right agent — planned, implemented, verified, reviewed — surfacing to you only
where a human decision genuinely matters.

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
in its own Git worktree. Every worker is a normal Claude session you can talk to directly
in the right-hand pane, and it inherits your user settings plus the target repository's
own `CLAUDE.md` and skills.

```
+------------------------------+----------------------------------------+
| Manager                      | Selected worker                        |
|  recent exchanges + input    |  attention banner                      |
+------------------------------+  transcript, follow-up input           |
| Workers / attention queue    |                                        |
+------------------------------+----------------------------------------+
```

## Setup

Requires Python 3.12+, `git`, and the `claude` CLI on your `PATH`.

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
```

## Run

```bash
./.venv/bin/python -m csm                        # launch the three-pane UI
./.venv/bin/python -m csm --register /path/repo  # register a repository at startup
CSM_BACKEND=scripted ./.venv/bin/python -m csm   # offline demo: no model calls
```

Press `?` in the app for the key bindings.

## Test

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/ruff check src tests
./.venv/bin/mypy
```

## Configuration

| Path / variable | Purpose |
| --- | --- |
| `~/.config/claude-session-manager/config.yaml` | preferences and model policy (see `config.example.yaml`) |
| `~/.local/share/claude-session-manager/csm.db` | durable state |
| `~/.local/share/claude-session-manager/worktrees/` | managed worktrees, never inside your repo |
| `CSM_HOME` | relocate the data directory |
| `CSM_CONFIG` | alternate config file |
| `CSM_STRONG_MODEL` / `CSM_FAST_MODEL` | default models per role |
| `CSM_BACKEND=scripted` | use the deterministic in-process backend |

## Safety

The application, not the agent, owns every destructive path. Worktrees live outside your
repository and only under the managed root; cleanup requires explicit confirmation and
refuses to remove a worktree holding uncommitted or unmerged work; branches are never
deleted; nothing pushes, force-pushes, or merges.

## Status

Working prototype, built for personal use. Architecture and conventions are in
[`CLAUDE.md`](CLAUDE.md); the original specification and the MVP verification record are
in [`docs/`](docs/).
