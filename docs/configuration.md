# Configuration

Everything is optional. Switchboard runs with no configuration file at all.

## Paths

| Path | Purpose | Override |
| --- | --- | --- |
| `~/.config/switchboard/config.yaml` | Preferences and model policy | `SB_CONFIG` |
| `~/.local/share/switchboard/` | Data directory | `SB_HOME` |
| `~/.local/share/switchboard/switchboard.db` | Durable state | follows `SB_HOME` |
| `~/.local/share/switchboard/worktrees/` | Managed worktrees, never inside your repo | follows `SB_HOME` |
| `~/.local/share/switchboard/runtime/` | tmux socket and per-runtime hook overlays | follows `SB_HOME` |
| `~/.local/share/switchboard/manager/` | The Manager's generation-bound MCP config | follows `SB_HOME` |
| `~/.local/share/switchboard/manager-workspace/` | The Manager's non-repository cwd | follows `SB_HOME` |
| `~/.switchboard/workflows/` | Your own workflow definitions | `SB_WORKFLOWS_DIR`, else `SB_HOME/workflows` |
| `<repo>/.switchboard/workflows/` | Workflows that travel with a repository | — |

`sb config` prints the first five of these plus the effective configuration, with
`claude.env` redacted. The Manager's MCP socket is not in this table: AF_UNIX paths are
length-limited on macOS, so it always lives at `/tmp/sb-manager-<runtime-id>.sock`.

Set `SB_HOME` to an isolated directory for any run that should not touch real state. That
is what the test suite does, and it is what you want for experiments.

## Environment variables

| Variable | Effect |
| --- | --- |
| `SB_HOME` | Relocate the whole data directory |
| `SB_CONFIG` | Use an alternate config file |
| `SB_WORKFLOWS_DIR` | Relocate the user workflow directory |
| `SB_BACKEND=scripted` | Use the deterministic in-process backend; no model is called |
| `SB_STRONG_MODEL` / `SB_FAST_MODEL` | Default model per role, when config says nothing |

## The configuration file

See [`config.example.yaml`](../config.example.yaml). Every key is optional.

```yaml
subagents:
  enabled: true                 # may workers spawn Claude's own helper subagents
  max_concurrent_per_worker: 3

commits:
  require_plan: true            # implementation needs an approved implementation contract

# Omit a role entirely to fall back to $SB_STRONG_MODEL / $SB_FAST_MODEL and then to
# whatever model Claude is already using. Writing `null` is not the same as omitting it:
# an explicit null pins the value and skips the environment fallback.
models: {}

permissions:
  writable_worker: acceptEdits  # acceptEdits | auto | manual | plan | null
  read_only_worker: plan

effort: {}                      # per role, e.g. `reviewer: high`. Null leaves it alone.

default_composite_workflow: complete-ticket

workflows:
  rebase-stack:
    preserve_merges: false
    autosquash_fixups: true
    never_force_push: true
  plan-feature:
    max_plan_lines: 10
  review-change:
    blocking_severities: [blocking, important]

claude:
  executable: null              # a wrapper such as `company-claude` instead of `claude`
  env: {}                       # extra environment, merged over the inherited one

worktree_bootstrap:
  files: []                     # gitignored files to copy into a new worktree
```

Notes on the ones with sharp edges:

- **`commits.require_plan`** is a safety gate, not a preference. Turning it off lets
  implementation start with no approved contract.
- **`claude.executable`** must be a real executable. A shell alias or shell function cannot
  be launched directly, and the error says so, because that is the mistake it most often
  catches. The parent environment is always inherited, so whatever a wrapper configures
  still applies; `claude.env` only adds to it. Nothing here can bypass managed policy — the
  wrapper is still the Claude CLI, and Switchboard only chooses which one to launch.
- **`worktree_bootstrap.files`** is empty by default. A worktree gets exactly what Git puts
  there, so something like `CLAUDE.local.md` is missing unless it is copied. Only files
  named here are copied, and only plain files directly inside the repository root — nothing
  is swept up by pattern, because these files are exactly where credentials tend to live.
- **`default_composite_workflow`** was previously called `default_profile`. The old key
  still loads. It is only the default: the Manager picks a workflow per request, and
  `investigate`, `diagnose-and-fix`, `rebase` and `review-only` are its equals.
- **`permissions`** is the knob that decides how often a fleet interrupts you. The default
  stops a writable worker asking about writes inside its own worktree while still asking
  about shell commands. `auto` hands command classification to Claude as well, which
  removes almost all remaining prompts; `manual` asks about everything. A workflow may
  override it with `permission_mode:`.

## Model, effort, and role selection

A worker's model comes from its role, resolved through `models.<role>` and falling back to
`models.general`; `effort` works the same way. Setting nothing at all means Switchboard
passes no `--model` or `--effort` and Claude uses whatever it is already configured to
use — which is usually what you want.

Roles are not a fixed list. A workflow declares the role its workers play, so
`models.investigator` or `effort.flake-hunter` works as soon as a workflow uses that name.

Every session is also launched with `--name`, so it appears under its job and role in
Claude's own `/resume` picker, Agent View, and `claude agents --json` — a worker stays an
ordinary session you could have started yourself.

## Claude settings inheritance

Workers launch in their repository or worktree and perform **normal** native Claude
discovery: user, managed/company, project, and project-local configuration all apply.
Switchboard deliberately omits `--setting-sources`. It adds one mode-0600 settings overlay
carrying its lifecycle hooks; it never replaces your settings and never selects a bypass
permission mode.

The Manager uses the same configured executable and environment, and the same native user
and managed discovery, from a dedicated non-repository workspace — so it inherits your
authentication and company policy but no project context. See [manager.md](manager.md).
