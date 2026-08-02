# Audit: the repository against the Manager / Workflow / Native Claude shift

The assessment required by
[`manager-workflow-native-claude-shift.md`](manager-workflow-native-claude-shift.md).
It classifies every subsystem, records the native-capability findings the shift asked for,
and says what was done. The "was / now" columns describe changes that have landed.

## Verdict in one paragraph

The three-layer model the spec wants — Manager, workflows, ordinary native workers — was
already the shape of the code. What was wrong was that **one workflow had been mistaken for
the architecture**. `complete-ticket`'s vocabulary had leaked out of its YAML and into the
domain: `JobStage` was its stage list, `WorkerRole` was its cast of characters, the "is this
finished?" gate hard-coded its four artifacts, and the Manager's own instructions named its
steps. A `rebase` or `investigate` job was expressible as a prompt but could never be
reported complete. Most of the shift is removing that leak, not adding machinery — and the
rest came from dogfooding, which found more than the reading did.

## Native Claude capability findings

Checked against the installed CLI rather than assumed.

| Capability | Finding | Verdict |
| --- | --- | --- |
| `claude --bg` + `claude agents` | `agents` takes **no subcommands** — `claude agents list` is rejected as "too many arguments". It offers `--json` and an interactive Agent View. There is no supported programmatic *send*, *attach*, *stop* or *turn-completion* interface for a background agent. | Cannot replace tmux. See below. |
| `claude agents --json` | Supported, needs no TTY, and reports `pid`, `cwd`, `kind`, `sessionId`, `name`, `status` for interactive **and** background sessions — including ones Switchboard launched. | Useful; naming makes it usable. |
| `-n` / `--name` | Shown in the prompt box, the `/resume` picker and the terminal title. | **DELEGATE** — every session is named after its job and role. |
| `--effort` | Per-session effort, a peer of `--model`. | **DELEGATE** — per-role configuration. |
| `--permission-mode` | `acceptEdits` / `auto` / `plan` / `manual` / … Writable workers previously ran with Claude's default and stopped on every file write. | **DELEGATE** — `acceptEdits` for writable workers, `plan` for read-only, both configurable and overridable per workflow. |
| Workspace trust | Recorded per **exact directory** in Claude's own state. Every writable worker gets a fresh worktree path, so every writable worker met the dialog. | See "the tedium" below. |
| `--session-id`, `--settings`, `--append-system-prompt`, `--mcp-config`, `--strict-mcp-config`, `--tools`, `--allowedTools`, `--disallowedTools`, hooks | Already used. | KEEP. |
| Subagents, Agent Teams, Dynamic Workflows, `/goal`, skills, memory | Worker-local. Claude owns them. | **DELEGATE** — and Switchboard stopped modelling the one it did. |
| Native OS sandbox | Would make read-only a sandbox rather than a tool policy. | **DEFER** — separate piece of work; recorded in [troubleshooting.md](troubleshooting.md). |

### Why native background sessions cannot replace the tmux runtime

The spec asked not to lose "reliable programmatic managed follow-ups/replies". That is what
`--bg` fails. Switchboard needs, simultaneously:

1. a **live interactive** session the user can drop into and type in;
2. **programmatic** delivery of a follow-up prompt into that same session;
3. a **correlated completion signal** per managed turn, so a run advances on a fact.

`claude --bg` gives (1) through Agent View and nothing supported for (2) or (3).
`claude -p --resume <id>` gives (2) and (3) but is a fresh process per turn that nobody can
sit inside while it runs. Only a persistent interactive process plus an input channel gives
all three, which is what the tmux runtime is. It stays, behind the existing `WorkerBackend`
boundary, so a future native equivalent can replace it without touching orchestration.

Dynamic Workflows remain ruled out for composite runs for the reasons already in
[architecture.md](architecture.md): no mid-run user input, resume only within one session,
and units that are subagents rather than sessions you can step into.

## Subsystem classification

### KEEP — uniquely Switchboard, or a stronger proven invariant

| Subsystem | Why it stays |
| --- | --- |
| `core/session_manager.py` | The single mutation point. Every invariant is here in ordinary Python. |
| `core/lineage.py` | "Which worktree *is* this job's change" has no native equivalent, and native `--worktree` would create competing lineage. |
| `core/evidence.py` | Deterministic completion. **Generalised**, not kept as-is — see below. |
| `core/runs.py`, `WorkflowRun` | Durable, human-gated, restart-surviving runs made of real sessions. |
| `core/transitions.py` | `ALLOWED_WORKER_TRANSITIONS` is cheap and has caught real bugs. |
| `gitops/` | Argv-only Git, managed-root worktrees, the refuse-to-discard cleanup decision. |
| `runtime/` | The only way to get all three properties above. |
| `agents/manager_mcp.py` authorization | Generation-bound authority; a stale pipe is not enough. |
| Workers never receive manager MCP config | Invariant 8. Unreachable beats discouraged. |
| Contracts, artifacts, freshness | Workflow primitives, and the reason delegation is checkable. |
| Attention queue and its ordering | Already event-driven off semantic transitions. |
| `routing/router.py` | The offline oracle. Not the production path, but it is what lets the whole orchestration path be tested without a model. |

### DELEGATE — Claude already does it reliably

| Was | Now |
| --- | --- |
| `Worker.active_helpers` counted a worker's subagents | Claude owns subagents; the count affected no durable contract. |
| Sessions visible only inside Switchboard | Launched with `--name`, so findable in Agent View, `/resume` and `claude agents --json`. |
| Effort fixed at the session default | `--effort`, per role, from config. |
| Writable workers stopped on every file write | `--permission-mode acceptEdits`, configurable, overridable per workflow. |

### BORROW — proven pattern from Agent Deck / CAO

| Concept | Source | What was taken |
| --- | --- | --- |
| Workflow validation before work starts | CAO | `sb workflows validate`: unknown step names, composite cycles, a step needing evidence no earlier step produces, a workflow no worker could run, one that requires what it produces, and a composite that could never be reported complete. |
| Native provider passthrough | CAO | Model, effort, permission mode and name delegated rather than re-invented. |
| Persistent conductor | Agent Deck | Validated the Manager; the stronger boundary (Manager proposes, deterministic state executes) is kept. |
| Explicit relationships, stable IDs | Agent Deck | Already true, and now extended: jobs link to a parent and to context jobs by UUID, never by name. |
| Event-driven attention | Agent Deck | Already true. The 1 Hz tick is a backstop, not the mechanism. |
| Jobs-first UX | Agent Deck | The board groups sessions under their job and shows workflow step progress. |
| Doorbell rule | Agent Deck | **DEFER.** No external integrations exist; the rule is recorded so one cannot be added wrongly later. |

### DELETE — redundant, with no lost guarantee

| Deleted | Replaced by |
| --- | --- |
| `JobStage` enum | `Job.stage: str`, declared by whichever workflow is running, and `Job.completed_at`, set only by the gate. |
| `JobStage.READY_TO_PUSH` special case | Nothing. A stage is a description; completion is computed. |
| `WorkerRole` as a closed enum | A validated, interned role *name*. A workflow may declare its own role and `role_policy`. |
| `READ_ONLY_ROLES` | The workflow's `mutates_code`, which was already the real source. An unanticipated role defaults read-only. |
| `ArtifactType.BEHAVIOR_CONTRACT` | `GOAL`, which carries the goal as well as its criteria, for every kind of request. |
| The if/elif harvest chain | A per-artifact-type dispatch table, so one turn can state its goal and its findings together. |
| `Worker.active_helpers` | Nothing. See DELEGATE. |
| `plan-feature` in the Manager's instructions | Choosing on a workflow's `definition_of_done`. |

### DEFER — good idea, no current need

- External integrations (GitHub, CI, Telegram) and the doorbell routing they would need.
- OS-sandboxed read-only workers.
- Replacing tmux with native background sessions — revisit when `claude agents` grows a
  supported send/stop interface.
- Multi-provider anything, generic memory, MCP/skill managers, web or mobile control
  planes, an arbitrary programmable workflow runtime. All explicit non-goals.

## The two architectural changes

### Completion became a function of the workflow

`ready_to_push` asked every job for an approved implementation contract, acceptance
criteria, fresh verification, a fresh review and a clean tree — always. That is
`complete-ticket`'s definition of done, written into the core.

A job's completion report is now derived from **what its workflow's unconditional steps
produce**. Each artifact type carries its own semantic check, a conditional step is never a
precondition (a step that may not run cannot be required), and any workflow that mutates
code additionally needs a clean authoritative tree. For `complete-ticket` this evaluates to
exactly the old rule — that equivalence is tested. For `rebase` it asks for a clean tree and
fresh checks. For `investigate` it asks for evidenced findings. A job following no workflow
is reported unfinished rather than vacuously done. No new configuration language: the
workflow already declared `produces`.

### Goal, criteria and evidence became the spine every request shares

Every request has a goal, whether it is a ticket, a question or an outage. `Goal` states it
and the criteria that would establish it; the evidence is a verification report, a findings
report, or a review. Questions and investigations now produce a durable `findings` artifact
with an answer that stands on its own — so the result of asking a Claude something outlives
the session and can be handed to the next one from the store, verbatim.

That is what makes the firefighting shape work as an ordinary workflow rather than a special
case: `diagnose-and-fix` runs diagnosis, fix, verification and review as four independent
sessions in one job, and `implement-fix` requires `findings`, so the ordering is enforced
rather than hoped for. When work genuinely belongs in separate jobs, `context_job_ids`
carries the evidence between them and `parent_job_id` keeps the parent incomplete until its
children are.

## What dogfooding found that reading did not

Each of these was a real stall in an end-to-end run against a real repository, and each is
fixed with a regression test.

1. **A first run killed the board.** Claude's workspace-trust dialog blocks `SessionStart`;
   the timeout propagated out of `start_or_recover` and the controller process died — taking
   down the Unix socket the Manager's MCP bridge connects to. The Manager survived in tmux
   with no orchestration tools and could only narrate that it had none.
2. **Workspace trust asked once per worktree.** Every writable worker gets a fresh path, so
   every writable worker stopped. Now the user vouches once per repository and Switchboard
   answers the dialog under four Python guards.
3. **An investigation produced nothing durable.** This drove the whole goal/criteria/evidence
   change above.
4. **Writable workers stopped on every file write.** Fixed by delegating the permission mode.
5. **Prerequisites ignored linked jobs**, which made decomposition impossible, and the
   missing-prerequisite error told every workflow to run `plan-feature`.
6. **Every run ended in a traceback per worker**: closing the store left the event pumps
   writing to a closed connection.
7. **The board said "nothing needs you"** about a session sitting on a permission prompt,
   because entering a worker clears its attention and leaving it put nothing back.
8. **A blocked *run* reached nothing at all.** Conservative reconciliation had stopped a
   `diagnose-and-fix` run, but both its sessions were idle and healthy, so the queue was
   empty and the summary said nothing needed me. Only asking the Manager directly found
   it. A stalled workflow being invisible is the one failure an operator replacement
   cannot have.
9. **A slow start was mistaken for a stuck one.** The sixth Claude in a fleet took longer
   than the startup wait to reach `SessionStart` because it was contending with five
   siblings; Switchboard declared it blocked and paused the run. Startup recovery now
   reads the pane, answers a trust dialog it can answer, and otherwise waits again — the
   cost of waiting is latency, the cost of a false "needs you" is a stalled fleet.

10. **Repeated steps left their sessions running.** A `fresh` step starts a new session
    each time it runs, and review sent the run back through verification twice, so the
    job ended with three live verifiers on one worktree — each holding a report a later
    commit had already invalidated. An earlier read-only attempt at the same role is now
    retired when its replacement starts. Writable sessions never are: one owns a worktree
    and *is* the job's change.

11. **A session could be entered and never left.** Attaching claims through the durable
    runtime, but releasing required a live session controller, which a disconnected
    worker does not have. Entering one flipped ownership to the human, paused its run and
    then raised on the way out, so the run stayed paused with no way back. Release now
    resolves the runtime exactly as the claim does.

Points 1, 2, 4, 8 and 9 have the same shape, and it is the shape that matters for this
product: **the fleet interrupting the user for something that did not need them, or
stopping without telling them.** Neither is visible from reading the code. Both are
obvious within two minutes of running it. Point 10 is the third form of the same thing:
the board has to stay readable, or none of the rest is reachable.
