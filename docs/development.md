# Developing Switchboard

## Setup

`sb` is installed with `uv tool install --editable .`, so it runs *this* checkout: source
edits are live immediately, but a `pyproject.toml` dependency change needs
`uv tool install --editable . --reinstall`.

The test tooling runs from a plain virtualenv, independent of the installed `sb`:

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
```

## Commands

```bash
./.venv/bin/python -m pytest -q   # full suite, 1-2 minutes
./.venv/bin/ruff check src tests
./.venv/bin/mypy
git diff --check
```

## The test tiers

A test belongs to exactly one tier.

| Tier | What it may use | What it proves |
| --- | --- | --- |
| `tests/unit/` | No subprocess, no git, no tmux | Routing, attention, transitions, freshness, prompts, workflow specs and loading, hook payloads, launch-argument composition, worktree path safety and bootstrap, schema migration, Claude-executable resolution |
| `tests/integration/` | Real SQLite, real Git repositories, real worktrees, `ScriptedWorkerBackend` | The whole orchestration path with no model call: workflows, composites, contracts, lineage, recovery, cleanup, the manager MCP |
| `tests/native/` | A real tmux server and a Claude-shaped fixture executable | Substrate claims nothing else can prove: exact-generation adoption, hook delivery and idempotency, entry and handback, rotation |
| `tests/ui/` | Textual's headless pilot | Board rendering, key bindings, auto-advance, entry from the board |

The native tier is **not** gated behind an environment variable — it runs as part of
`pytest -q` and needs `tmux` installed. It never calls a paid model: `tests/fixtures/`
holds a fake native Claude that emits Claude-shaped hook payloads, and a fake interactive
program for raw tmux control.

Every fixture is rooted in a per-test temporary `SB_HOME`, and every Git operation runs
against a real temporary repository. Git is never mocked.

## Verification expectations

"Tests pass" is not evidence. Verify the observable behaviour the change is about, as
end-to-end as feasible, and rerun it against current HEAD after any rebase or review fix.
A fix should be shown to fail without its change — reverting only that change and rerunning
the specific test is cheap and catches tests that would have passed either way. State
clearly anything that could not be tested.

## Commits

Clean, logical, atomic commits: one coherent, independently understandable change each. No
giant catch-all commits, no meaningless micro-commits. Plan the commit stack before
substantial implementation, and leave `git status` clean.

Never push, force-push, merge, discard work, delete branches, or destructively remove
worktrees without being asked. Be especially conservative with dirty or unpushed worktrees.

## Changing the schema

Bump `SCHEMA_VERSION` in `storage/database.py` and extend `migrate()`. Every statement is
`IF NOT EXISTS`, and versions so far only add tables and columns that default cleanly, so
replaying the schema is the whole migration. Anything that needs to rewrite existing rows
gets an explicit step — see `_reconcile_open_native_turns` for the shape.

Renaming a persisted field is a compatibility question, not just a rename: stored rows carry
the old key. `Job.composite_workflow` shows the pattern — a pydantic `AliasChoices` and a
regression test that loads a row written under the old name.

## Running against real Claude

```bash
SB_HOME=/tmp/sb-experiment sb --register /path/to/disposable/repo
```

Use a disposable repository and an isolated `SB_HOME`. Native runtimes deliberately survive
the board quitting, so an experiment leaves tmux servers and Claude processes behind; see
[troubleshooting.md](troubleshooting.md#orphaned-runtimes).

## Where things live

[architecture.md](architecture.md) has the module map and the rule for deciding where a new
behaviour belongs. [runtime.md](runtime.md) covers the tmux substrate and hooks,
[workflows.md](workflows.md) the workflow and contract engine, [manager.md](manager.md) the
native Manager and its MCP.
