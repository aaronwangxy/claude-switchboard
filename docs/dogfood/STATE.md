# Dogfood state

Working memory for the autonomous dogfood loop (`.claude/loop.md`). Successive shifts read
and rewrite this file. It describes **now** — prune anything no longer true. It is not a
changelog; `git log` is. It is not the frozen field record; that is
[`../dogfood-report.md`](../dogfood-report.md).

Last shift: 2026-08-03 (overnight).

## Read this first

**There is still un-submitted text in the live Manager's composer:**

> `Make the scratchpad writable instead of plan mode`

Not mine, not submitted. It contradicts the approved plan (read-only), and `send_managed`
types into that same composer — the next managed send would be concatenated with it and
submitted as one prompt. Left in place because it is your own input in your own session.
Clear it or send it deliberately before messaging the Manager.

You committed a README change to `main` seconds before my commit landed. Both are pushed.

## Active work — resume this first

**Scratchpad session feature, alive and parked. Unchanged from the last shift.**

- Board: tmux session `sb2` on the default socket, `SB_HOME` =
  `/tmp/claude-501/-Users-aaron-dev-claude-switchboard/726099bb-b2d5-4be5-8889-7a70cb465016/scratchpad/home`.
  Worker/manager panes on `/private/tmp/switchboard-tmux-7ceb179de564c6854bc8.sock`.
- Job "Board keyboard shortcut to open an independent scratchpad Claude session",
  `complete-ticket` **step 2/8 `implement-approved-plan`**. Commit 1 of 4 landed; commit 2
  is in flight in the implementer's own worktree.
- The implementer is still on the same Bash permission prompt for a focused
  pytest/ruff/mypy command. It has not moved. **The prompt rate is the blocker and it is
  the decision below** — it is not something a shift can grind through, and I did not try.
- That board is running code from before the last two shifts. Restarting it to adopt the
  fixes would mean adopting live runtimes with your experiment mid-commit; still not worth
  it.

## Landed this shift

`fd1ee0c fix(runtime): never write a runtime snapshot back across a tmux round trip`.

`set_owner` and `terminate` read the runtime row, spent a tmux subprocess call, then wrote
that pre-call snapshot back. Claude's hooks commit from their own process throughout, so
whatever they wrote during the call was erased — and a hook fires once. Both now re-read
and change only the field they own.

The failure a user meets: answer a permission prompt, watch the turn finish, press detach.
Stop lands while tmux is handing ownership back, `TURN_COMPLETE` is overwritten with the
snapshot's `WAITING`, the backend never acknowledges the turn, the runtime never returns to
`READY` — and a runtime that is not `READY` refuses every send. The worker is stuck
`BLOCKED` on an answered prompt with its input lane shut.

### Evidence

- Three tests, each shown failing without the change and passing with it: two unit tests
  over the real supervisor and a real second SQLite connection, and one native-tier test
  through real tmux and the Claude-shaped fixture that drives the whole attach → answer →
  detach path and asserts the worker comes back unblocked.
- 451 passed, ruff and mypy clean at HEAD.
- No independent agent review: spawning subagents is disallowed in this harness.

## Resolved: the startup stall that was filed as unexplained

The earlier note said a healthy session was declared blocked on a `SessionStart` that had
already arrived, "same symptom as `0b92ea0`, different mechanism". **It is not a different
mechanism.** It is exactly the bug `0b92ea0` fixed, observed on a board process that
predated the fix:

- Incident (real `SB_HOME`, runtime `0c7f2d4d`): launched 19:35:27.6 UTC, `SessionStart`
  committed 19:35:29.8, `_wait_ready` timed out 19:36:27.9, failed 19:38:28.
- `0b92ea0` was committed at 14:52 CDT = 19:52 UTC — **17 minutes after the incident**.
- The board process serving that `SB_HOME` started ~14:34 CDT, i.e. at `b1500b0`. At
  `b1500b0`, `supervisor._record` wrote the caller's snapshot without re-reading, and
  `_watch` calls `supervisor.observe` every 0.5 s — read, tmux subprocess, write. A
  `SessionStart` landing in that window is erased permanently.

Lesson worth keeping: **`sb` is editable, but a running board is not.** When judging an
incident, date it against the code the process actually loaded, not against `HEAD`. That
mistake cost a shift.

Cross-process SQLite visibility was correctly eliminated; I re-probed it (a poller on one
connection sees a hook subprocess's commit within 50 ms) and it is not involved.

## Needs your decision — unchanged, and still blocking

A writable worker cannot reach its own verification evidence unattended:

- `permissions.writable_worker = "acceptEdits"` covers edits, **not Bash**. Every command
  stops the worker.
- Claude's "don't ask again" is scoped to the exact command string, so it does not
  generalise across test invocations.
- A heredoc commit message is refused a rule at all — *"Contains shell syntax that cannot
  be statically analyzed"* — and the workflow's commit step uses one.
- A fresh worktree has no `.venv`, so the documented `./.venv/bin/python -m pytest` costs
  two more prompts before any test runs.

The partial fix is a checked-in `.claude/settings.json` prefix allow list for exactly the
commands `CLAUDE.md` already documents. **My harness refuses to commit it, correctly: an
agent should not grant itself permissions.** It does nothing for the heredoc commit step.
The complete answer is probably moving `permissions.writable_worker` off `acceptEdits`,
which is a real change to the product's safety posture and yours to make.

Unverified: that a project-scope `.claude/settings.json` layers under Switchboard's
per-runtime `--settings` overlay. Documented, not evidenced — I cannot nest a `claude`
process to test it.

## Leaked processes, for when you next tidy up

**36 native `claude` processes and 11 tmux servers are alive** from days of dogfooding.
Some belong to live boards; most are orphans. Concretely, `claude` pid 94524 has been
sitting idle for nine hours — it is runtime `0c7f2d4d` from the stall above, its board and
even its tmux server long gone, the process reparented and never reaped.

Not mine to kill. Worth knowing that nothing reclaims a native session when its board dies,
and that this is what "the user can enter any worker" costs if a board exits badly.

The dead attempt on the real `SB_HOME` is the same incident: worker `70fe0109` blocked, run
paused at step 0, two unhandled attention items. Retire it from the board when convenient.

## What this harness would not let me do

- `tmux send-keys` into a worker pane — allowed early in a shift, blocked later. Driving an
  implementer to completion by hand is not something a shift can rely on.
- Committing a permissions allow list, or building a prompt auto-answerer — refused, and
  rightly.
- Spawning subagents, so no independent review of anything a shift lands.

## Rejected

- **Restarting the live board to adopt this shift's fix.** The implementer is mid-commit.
- **Killing the 36 stranded `claude` processes.** Your sessions, your call.
- **Clearing the stray Manager composer line.** Your input; the guardrail is explicit.
- **Giving the scratchpad its own worktree.** A throwaway session should not leave a branch
  and a cleanup ritual behind.

## Open questions

- Are there other read → subprocess → write windows left? I swept every `save_runtime`
  call site this shift and the supervisor's two were the last with a tmux call in the
  middle. `session_manager.attach`/`detach` re-read, but note that their re-read does not
  protect them — the damage had already been committed one frame deeper, inside
  `set_owner`. A re-read only helps the method that owns the write.
- Is `AskUserQuestion` a tool workers should have at all? It costs a real
  `PermissionRequest` like any other tool, but one per planning step rather than dozens.
- A permission answered mid-command still shows `blocked` until the *next* tool starts, so
  a single three-minute `pytest` lags. Only `PostToolUse` would close that, and mapping it
  would double every tool in the transcript. Left alone; revisit only if the lag is
  actually observed to mislead the Manager.
- Does the composite engine earn its keep now that Claude Code has dynamic workflows?
  Untested since `docs/architecture.md` argued yes.
- Read-only is a tool policy, not a sandbox (workers keep Bash). Does that matter in
  practice?
- How much of the 2500-line `SessionManager` is load-bearing after the recent fixes?

## Research notes

None yet. Adjacent systems worth a shift: Claude Code's own agent teams and dynamic
workflows, Agent Deck, AWS agent/CLI orchestrators. Produce hypotheses to test here, not
features to copy.
