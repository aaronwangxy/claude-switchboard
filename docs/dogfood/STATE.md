# Dogfood state

Working memory for the autonomous dogfood loop (`.claude/loop.md`). Successive shifts read
and rewrite this file. It describes **now** — prune anything no longer true. It is not a
changelog; `git log` is. It is not the frozen field record; that is
[`../dogfood-report.md`](../dogfood-report.md).

Last shift: 2026-08-03 (early morning).

## Read this first

**There is still un-submitted text in the live Manager's composer:**

> `Make the scratchpad writable instead of plan mode`

Not mine, not submitted. `send_managed` types into that same composer, so the next managed
send would be concatenated with it and submitted as one prompt. Left in place because it is
your own input in your own session. Clear it or send it deliberately before messaging the
Manager.

## Active work — resume this first

**Scratchpad session feature, alive and parked. Still where the last shift left it.**

- Board: tmux session `sb2` on the default socket (pid 25800), `SB_HOME` =
  `/tmp/claude-501/-Users-aaron-dev-claude-switchboard/726099bb-b2d5-4be5-8889-7a70cb465016/scratchpad/home`.
  Worker/manager panes on `/private/tmp/switchboard-tmux-7ceb179de564c6854bc8.sock`.
- Job "Board keyboard shortcut to open an independent scratchpad Claude session",
  `complete-ticket` **step 2/8 `implement-approved-plan`**. Commit 1 of 4 landed; commit 2
  is in flight in the implementer's own worktree.
- The implementer has been on one Bash permission prompt since 00:07 UTC — a focused
  `pytest && ruff && mypy` command. Before that it raised **seven prompts in sixteen
  minutes** (23:51–00:07). The prompt rate is the blocker.
- That board is running code from before the last three shifts. Its fourteen unhandled
  `permission_required` items, in Bash/tool pairs, are the pre-`3041372` duplicate and the
  pre-`9bd0b68` failure to clear an answered prompt. Both are fixed at HEAD; the board
  predates them (started 14:54 CDT, the fixes landed 19:29 CDT). **Not a regression — do
  not re-investigate.**
- Restarting the board to adopt HEAD would mean adopting live runtimes with the experiment
  mid-commit. Still not worth it.

To actually finish this job, set `permissions.writable_worker_allow` (below) and start a
fresh board on HEAD. The old one cannot be rescued cheaply.

## Landed this shift

`debb2a0 feat(runtime): let the user clear a command for unattended worker work`.

`permissions.writable_worker_allow` — a list of native permission rules carried in
Switchboard's per-runtime settings overlay for writable workers. Empty by default, so the
posture is unchanged until you fill it in. Suggested starting point is commented out in
`config.example.yaml`.

### The finding that made it the right shape

Three shifts assumed the partial fix was a checked-in `.claude/settings.json` allow list.
**That would never have worked.** Measured against the real `claude` CLI:

- A `permissions.allow` entry in a `--settings` overlay is honoured, and is **not** subject
  to workspace trust.
- A `permissions.allow` entry in the *directory's own* `.claude/settings.json` is **ignored
  until that directory is trusted** — Claude says so out loud: *"Ignoring 1
  permissions.allow entry from .claude/settings.json: this workspace has not been
  trusted."*
- Every writable worker runs in a per-worker worktree. `~/.claude.json` has 26 project
  entries and **zero** under any `/worktrees/` path, including the live implementer's own
  cwd after a full job ran in it. Worker worktrees are never trusted, so repository allow
  rules are dead there — silently.

Settling STATE's open question: the overlay **layers**, it does not replace. Claude read the
project file, counted its entries, and suppressed them for trust — while honouring the
overlay in the same run.

### Evidence

- Probe with real `claude -p` in a scratch repo: rule in the overlay → command ran; same
  script with a rule only in `.claude/settings.json` → denied; third script with no rule →
  denied. Two clean controls.
- End-to-end through Switchboard's own code: generated the overlay by calling
  `NativeClaudeRuntime._write_settings` with the config set, ran real `claude --settings`
  against it — allowed command ran unprompted, control denied, and the lifecycle hooks
  still fired alongside the permissions block.
- One unit test shown failing before the change; three total (default empty, writable
  configured, read-only granted nothing).
- 454 passed, ruff and mypy clean at HEAD. One earlier full-suite run had
  `tests/ui/test_board.py::test_selecting_a_worker_shows_durable_orchestration_state` fail
  on `NoMatches: #manager-status`; it passed alone, the whole UI tier passed both with and
  without the change, and a second full run was green. Textual pilot flake under full-suite
  load — worth watching, not chased.
- No independent agent review: spawning subagents is disallowed in this harness.

## Still needs your decision

`writable_worker_allow` gives you the mechanism; it does not choose the policy.

- **What to put in it.** The commands `CLAUDE.md` already documents are the honest
  candidates. I did not seed them: an agent should not decide which commands it may run
  unattended. This is now a one-line config edit rather than an impossibility.
- **The heredoc commit step is not covered.** Claude refuses to write *any* rule for a
  heredoc commit message — *"Contains shell syntax that cannot be statically analyzed"* —
  and `complete-ticket`'s commit step uses one. A rule cannot fix that; the workflow's
  commit prompt would have to stop using a heredoc. Worth a shift.
- **`permissions.writable_worker` off `acceptEdits`** is still available and still a real
  posture change. The allow list is the narrower move; take it first.

Stale note now retired: a fresh worktree does have a `.venv` (the live implementer's does),
so that was never the extra cost it was written up as.

## Leaked processes, for when you next tidy up

**35 native `claude` processes** and 8 tmux sockets are alive from days of dogfooding. Most
are orphans — nothing reclaims a native session when its board dies. Not mine to kill.

The dead attempt on the real `SB_HOME` is still there: one blocked planner, run paused at
step 0, two unhandled attention items. Retire it from the board when convenient.

## What this harness would not let me do

- `tmux send-keys` into a worker pane — allowed early in a shift, blocked later. Driving an
  implementer to completion by hand is not something a shift can rely on.
- Committing a permissions allow list *for this repository*, or building a prompt
  auto-answerer — refused, and rightly. Note the distinction from what landed: shipping the
  mechanism with an empty default is a product capability; filling it in is your call.
- Spawning subagents, so no independent review of anything a shift lands.

## Rejected

- **Grinding the implementer's prompts by hand.** Seven prompts in sixteen minutes with
  four commits to go. Produces no durable improvement, and the harness blocks it anyway.
- **Seeding `writable_worker_allow` with defaults.** That is the agent granting itself
  permissions through a wider door.
- **Restarting the live board to adopt this shift's fix.** The implementer is mid-commit.
- **Killing the stranded `claude` processes.** Your sessions, your call.
- **Clearing the stray Manager composer line.** Your input; the guardrail is explicit.

## Open questions

- Should Switchboard trust a worker's worktree the way it trusts a repository? Invariant 15
  answers a *trust dialog* in a pane; it evidently does not leave a durable trust entry for
  the worktree, since none exists for any. If it did, repository-level settings would start
  applying to workers — which is either the right unification or a quiet widening. Untested
  either way.
- Is `AskUserQuestion` a tool workers should have at all? It costs a real
  `PermissionRequest` like any other tool, but one per planning step rather than dozens.
- A permission answered mid-command still shows `blocked` until the *next* tool starts, so
  a single three-minute `pytest` lags. Only `PostToolUse` would close that, and mapping it
  would double every tool in the transcript. Left alone; revisit only if the lag is
  actually observed to mislead the Manager.
- Are there other read → subprocess → write windows left? The supervisor's two were the
  last with a tmux call in the middle. A re-read only protects the method that owns the
  write.
- Does the composite engine earn its keep now that Claude Code has dynamic workflows?
  Untested since `docs/architecture.md` argued yes.
- Read-only is a tool policy, not a sandbox (workers keep Bash). Does that matter in
  practice?
- How much of the 2500-line `SessionManager` is load-bearing after the recent fixes?

## Research notes

None yet. Adjacent systems worth a shift: Claude Code's own agent teams and dynamic
workflows, Agent Deck, AWS agent/CLI orchestrators. Produce hypotheses to test here, not
features to copy.
