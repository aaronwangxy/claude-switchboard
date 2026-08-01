# Claude Session Manager

A one-window control plane for multiple independent Claude coding sessions.

Paste a ticket, ask a question, or say "rebase this" into one manager input. The
manager routes it: to an existing worker, to a reusable workflow, or to a new
independent worker in its own Git worktree. Every worker is a normal Claude
session you can talk to directly in the right-hand pane.

The full product and implementation specification is
[`CLAUDE_SESSION_MANAGER_GOAL.md`](CLAUDE_SESSION_MANAGER_GOAL.md).
Verification evidence is in [`MVP_EVIDENCE.md`](MVP_EVIDENCE.md).

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

## Where things live

| Path | Purpose |
| --- | --- |
| `~/.local/share/claude-session-manager/csm.db` | durable state (override with `CSM_HOME`) |
| `~/.local/share/claude-session-manager/worktrees/` | managed worktrees, never inside your repo |
| `~/.config/claude-session-manager/config.yaml` | preferences and model policy (see `config.example.yaml`) |
