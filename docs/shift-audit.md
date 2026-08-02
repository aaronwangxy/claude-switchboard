# Audit: the repository against the Manager / Workflow / Native Claude shift

The assessment required by
[`manager-workflow-native-claude-shift.md`](manager-workflow-native-claude-shift.md).
It classifies every subsystem, records the native-capability findings the shift asked for,
and says what was done about each. Written against the repository as it stood before the
shift; the "Outcome" column is what the shift actually changed.

## Verdict in one paragraph

The three-layer model the spec wants — Manager, workflows, ordinary native workers — was
already the shape of the code. What was wrong was that **one workflow had been mistaken for
the architecture**. `complete-ticket`'s vocabulary had leaked out of its YAML and into the
domain: `JobStage` was its stage list, `WorkerRole` was its cast of characters, and the
"is this finished?" gate hard-coded its four artifacts. A `rebase` or `investigate` job was
expressible as a prompt but could never be reported complete, because completion meant
"has an approved implementation contract, acceptance criteria, verification and review".
Most of the shift is removing that leak, not adding machinery.

## Native Claude capability findings

These were checked against the installed CLI rather than assumed.

| Capability | Finding | Verdict |
| --- | --- | --- |
| `claude --bg` + `claude agents` | `agents` takes **no subcommands** — `claude agents list` is rejected as "too many arguments". It offers `--json` (a listing) and an interactive Agent View. There is no supported programmatic *send*, *attach*, *stop* or *turn-completion* interface for a background agent. | Cannot replace tmux. See below. |
| `claude agents --json` | Supported, does not require a TTY, and reports `pid`, `cwd`, `kind`, `sessionId`, `name`, `status` for interactive **and** background sessions — including sessions Switchboard launched itself. | **BORROW** as an independent liveness cross-check. |
| `-n` / `--name` | Sets a display name shown in the prompt box, the `/resume` picker and the terminal title. | **DELEGATE** — name every session after its job and role. |
| `--effort` | Per-session effort level (`low`…`max`), a peer of `--model`. Switchboard configured models per role but not effort. | **DELEGATE** — config, per role. |
| `--session-id`, `--settings`, `--append-system-prompt`, `--mcp-config`, `--strict-mcp-config`, `--tools`, `--allowedTools`, `--disallowedTools`, hooks | Already used. | KEEP. |
| `--permission-mode`, native sandbox | Plumbed but unused for workers; read-only is still tool policy plus prompt. | **DEFER** — a real fix needs the OS sandbox, which is a separate piece of work. Recorded in [troubleshooting.md](troubleshooting.md#known-limitations). |
| Subagents, Agent Teams, Dynamic Workflows, `/goal`, skills, memory | Worker-local. Claude owns them. Switchboard should not model them. | **DELEGATE** — and delete the one place it did. |

### Why native background sessions cannot replace the tmux runtime

The spec asked not to lose "reliable programmatic managed follow-ups/replies". That is the
requirement `--bg` fails. Switchboard needs, simultaneously:

1. a **live interactive** session the user can drop into and type in;
2. **programmatic** delivery of a follow-up prompt into that same session;
3. a **correlated completion signal** for each managed turn, so a run advances on a fact.

`claude --bg` gives (1) through Agent View and nothing supported for (2) or (3).
`claude -p --resume <id>` gives (2) and (3) but is a fresh process per turn that nobody can
sit inside while it runs. Only a persistent interactive process plus an input channel
provides all three, which is what the tmux runtime is. It stays, behind the existing
`WorkerBackend` boundary so a future native equivalent can replace it without touching
orchestration. Recorded in [runtime.md](runtime.md).

Dynamic Workflows remain ruled out for composite runs for the reasons already in
[architecture.md](architecture.md): no mid-run user input, resume only within one session,
and units that are subagents rather than sessions you can step into.

## Subsystem classification

### KEEP — uniquely Switchboard, or a stronger proven invariant

| Subsystem | Why it stays |
| --- | --- |
| `core/session_manager.py` | The single mutation point. Every invariant is here in ordinary Python. |
| `core/lineage.py` | "Which worktree *is* this job's change" has no native equivalent, and native `--worktree` would create competing lineage. |
| `core/evidence.py` | Deterministic completion. **Generalized**, not kept as-is — see below. |
| `core/runs.py`, `WorkflowRun` | Durable, human-gated, restart-surviving composite runs made of real sessions. |
| `core/transitions.py` | `ALLOWED_WORKER_TRANSITIONS` is cheap and has caught real bugs. |
| `gitops/` | Argv-only Git, managed-root worktrees, the refuse-to-discard cleanup decision. |
| `runtime/` (tmux, supervisor, hook bridge, native turns) | The only way to get all three properties above. See the finding. |
| `agents/manager_mcp.py` authorization | Generation-bound authority; a stale pipe is not enough. |
| Workers never receive manager MCP config | Invariant 8. Unreachable beats discouraged. |
| Contracts, artifacts, freshness | Workflow primitives, and the reason delegation is checkable. |
| Attention queue and its ordering | Already event-driven off semantic transitions. |

### DELEGATE — Claude already does it reliably

| Was | Now |
| --- | --- |
| `Worker.active_helpers` — Switchboard counted a worker's subagents and painted the number | Deleted. Claude owns subagents; the count affected no durable contract. |
| Session identity visible only inside Switchboard | Every session is launched with `--name`, so it is findable in Agent View, `/resume` and `claude agents --json` without Switchboard. |
| Effort fixed at the model default | `--effort`, per role, from config. |
| Liveness known only through tmux | tmux stays authoritative; `claude agents --json` is consulted as a supported second opinion during recovery. |

### BORROW — proven pattern from Agent Deck / CAO

| Concept | Source | What was taken |
| --- | --- | --- |
| Workflow validation before work starts | CAO | `sb workflows validate`: unknown step names, composite cycles, unsatisfiable `requires`, role/`mutates_code` mismatches, prompts referencing tokens nothing supplies. |
| Native provider passthrough | CAO | Effort/model/name delegated rather than re-invented; no Switchboard-specific equivalents. |
| Persistent conductor | Agent Deck | Validated the Manager; the stronger boundary (Manager proposes, deterministic state executes) is kept. |
| Explicit relationships, stable IDs | Agent Deck | Already true — jobs, workers, runs and worktrees are UUID-related, never name-related. No change needed. |
| Event-driven attention | Agent Deck | Already true. The board's 1 Hz tick is a backstop, not the mechanism. |
| Jobs-first UX | Agent Deck | The board now groups sessions under their job and shows workflow step progress. |
| Doorbell rule | Agent Deck | **DEFER.** No external integrations exist yet; the rule is recorded so one cannot be added wrongly later. |

### DELETE — redundant, with no lost guarantee

| Deleted | Replaced by |
| --- | --- |
| `JobStage` enum | `Job.stage: str`, declared by whichever workflow is running. `complete-ticket` still says `planning`/`implementing`/`verifying`; `rebase` says `rebasing`. |
| `JobStage.READY_TO_PUSH` special case in `_advance_stage` | `finishes_job: true` on a workflow, gated by the general completion report. |
| `WorkerRole` as a closed enum | A validated role *name*. Built-in policies stay keyed by name; a workflow may declare its own role and its own `role_policy`. |
| `READ_ONLY_ROLES` | Writability comes from the workflow's `mutates_code`, which was already the real source for every workflow-started worker. |
| Hardcoded `WORKFLOW_PHRASES` / `DEFAULT_COMPOSITE_WORKFLOW` in the router | Derived from the registry and config, so a user workflow is routable the day it is written. |
| `Worker.active_helpers` | Nothing. See DELEGATE. |

### DEFER — good idea, no current need

- External integrations (GitHub, CI, Telegram) and the doorbell routing they would need.
- OS-sandboxed read-only workers.
- Replacing tmux with native background sessions — revisit when `claude agents` grows a
  supported send/stop interface.
- Multi-provider anything, generic memory, MCP/skill managers, web or mobile control
  planes, an arbitrary programmable workflow runtime. All explicit non-goals.

## The one architectural change

Everything above is bookkeeping next to this: **completion stopped being a fixed checklist
and became a function of the workflow being run.**

Before, `ready_to_push` asked for an approved implementation contract, acceptance criteria,
fresh verification, a fresh review, and a clean tree — always, for every job. That is
`complete-ticket`'s definition of done, written into the core.

Now a job's completion report is derived from **what its workflow's steps actually
produce**. Each artifact type carries its own semantic check (a contract must be approved
and free of blocking decisions; criteria must have passed or carry an accepted limitation;
a review must have no unresolved blocking findings), and any workflow that mutates code
additionally requires a clean authoritative tree. For `complete-ticket` this evaluates to
exactly the old rule — that equivalence is tested. For `rebase` it asks for a clean tree and
fresh verification and nothing about plans. For `investigate` it asks for the findings
report. No new configuration language: the workflow already declared `produces`.

That is what makes the spec's last success criterion true for more than one workflow:

> Switchboard tells me when the work is actually complete.
