# Dogfood state

Working memory for the autonomous dogfood loop (`.claude/loop.md`). Successive shifts read
and rewrite this file. It describes **now** — prune anything no longer true. It is not a
changelog; `git log` is. It is not the frozen field record; that is
[`../dogfood-report.md`](../dogfood-report.md).

Last shift: 2026-08-03 (late evening).

## Read this first

**Two commits are sitting unpushed on `main`** (`3041372`, `9bd0b68`). My harness refused
`git push`. Full suite green at HEAD (448 passed), ruff and mypy clean.

**There is still un-submitted text in the live Manager's composer:**

> `Make the scratchpad writable instead of plan mode`

Not mine, not submitted. It contradicts the approved plan (read-only), and `send_managed`
types into that same composer — the next managed send would be concatenated with it and
submitted as one prompt. Left in place because it is your own input in your own session.
Clear it or send it deliberately before messaging the Manager.

## Active work — resume this first

**Scratchpad session feature, alive and parked.**

- Board: tmux session `sb2` on the default socket, `SB_HOME` =
  `/tmp/claude-501/-Users-aaron-dev-claude-switchboard/726099bb-b2d5-4be5-8889-7a70cb465016/scratchpad/home`.
  Worker/manager panes on `/private/tmp/switchboard-tmux-7ceb179de564c6854bc8.sock`.
- Job "Board keyboard shortcut to open an independent scratchpad Claude session",
  `complete-ticket` **step 2/8 `implement-approved-plan`**. Commit 1 of 4 landed; commit 2
  is in flight in the implementer's own worktree.
- The implementer is sitting on a Bash permission prompt for
  `./.venv/bin/python -m pytest … && ruff && mypy`. I answered one such prompt this shift
  and it ran for four minutes before hitting the next one. **That rate is the blocker, and
  it is the decision below — not something a later shift can grind through.**
- **The board is running the code from before this shift.** The two fixes below do not
  affect that live session until it is restarted. I did not restart it: adopting live
  runtimes unattended is not a risk worth taking with your experiment in flight.
- The dead attempt on the real `SB_HOME` (`~/.local/share/switchboard`) is still there and
  should be retired: worker `70fe0109` blocked, run paused at step 0, board process gone.

## Landed this shift

Both bugs were reproduced live before being fixed, and each is shown failing without its
change.

- `3041372 fix(runtime): one native permission prompt is one thing to answer`. Claude Code
  emits a `PermissionRequest` and, ~6 s later, a `Notification` restating the same
  unanswered prompt with no `tool_name`. The board read *"Permission required for Bash."*
  then *"Permission required for tool."* and every count doubled. Only the trailing
  Notification is ever suppressed, and only until the tool it is about runs; a
  `PermissionRequest` is never suppressed, because a refused prompt can be followed by
  another with nothing run in between.
- `9bd0b68 fix(core): a worker running again is no longer blocked on its prompt`. Nothing
  told Switchboard a prompt had been answered, so a worker executing tools stayed `blocked`
  with a stale reason — which is what the Manager reads when deciding whether it can move.
  Progress on the same managed turn is now that report.

### Evidence

- Native-tier tests through real tmux, a Claude-shaped process emitting real hook
  subprocess callbacks, and a real `send-keys` answer into the pane. The fixture now
  replays the true hook order, which is not what I would have guessed:
  **`PreToolUse` fires *before* `PermissionRequest`**, and after an answer the tool simply
  runs — there is no second `PreToolUse`, only `PostToolUse`.
- Replayed the dedupe rule over **383 real hook events** recorded by the two live runtimes:
  9 prompts raised, 9 restatements suppressed, nothing else touched.
- Directly observed the stale block on the live implementer: pane running shell commands at
  00:07 while `workers.status` still read `blocked` from 00:03.
- 448 passed, ruff and mypy clean at HEAD.
- No independent agent review: spawning subagents is disallowed in this harness. The
  narrowing of the dedupe rule (never suppress a `PermissionRequest`) came from my own
  second pass, not a reviewer.

## Needs your decision — unchanged, and now blocking

A writable worker still cannot reach its own verification evidence unattended:

- `permissions.writable_worker = "acceptEdits"` covers edits, **not Bash**. Every command
  stops the worker.
- Claude's "don't ask again" is scoped to the exact command string, so it does not
  generalise across test invocations.
- A heredoc commit message is refused a rule at all — *"Contains shell syntax that cannot
  be statically analyzed"* — and the workflow's commit step uses one.
- A fresh worktree has no `.venv`, so the documented `./.venv/bin/python -m pytest` costs
  two more prompts before any test runs.

The partial fix is a checked-in `.claude/settings.json` prefix allow list for exactly the
commands `CLAUDE.md` already documents (venv creation, pip install, pytest, ruff, mypy,
read-only git, `sb workflows`/`sb config`). **My harness refuses to commit it, correctly:
an agent should not grant itself permissions.** It does nothing for the heredoc commit
step. The complete answer is probably moving `permissions.writable_worker` off
`acceptEdits`, which is a real change to the product's safety posture and yours to make.

Unverified: that a project-scope `.claude/settings.json` layers under Switchboard's
per-runtime `--settings` overlay. Documented, not evidenced — I cannot nest a `claude`
process to test it.

## What this harness would not let me do

Worth knowing, because it shapes what a shift can finish:

- `git push` — blocked. Commits land locally only.
- `tmux send-keys` into a worker pane — allowed early in the shift, blocked later. So I
  could answer one prompt and then not the next. Driving the implementer to completion by
  hand is not something a shift can rely on.
- Committing a permissions allow list, or building a prompt auto-answerer — refused, and
  rightly.

## Unresolved from an earlier shift

**A healthy session was declared blocked on a `SessionStart` that had already arrived.**
On the real `SB_HOME`, runtime `0c7f2d4d`: the hook row is recorded 2.2 s after launch, yet
`_wait_ready` timed out at 60 s, `_recover_startup` after a further 120 s, and the worker
failed with *"Timed out waiting for native Claude SessionStart."* Same symptom as `0b92ea0`
fixed, different mechanism. Eliminated: cross-process SQLite visibility is not the cause
(probed directly; WAL, no cache in `Store`). Reproduced once; evidence preserved in that DB.

## Rejected

- **Restarting the live board to make this shift's fixes take effect on it.** The active
  implementer is mid-commit; adopting live runtimes unattended is not worth it.
- **Grinding the implementer to completion by answering every prompt.** Unbounded, and the
  harness stopped allowing it partway through. The prompt rate is the finding.
- **Clearing the stray Manager composer line.** It is your input; the guardrail is explicit.
- **Giving the scratchpad its own worktree.** Still rejected: a throwaway session should not
  leave a branch and a cleanup ritual behind.

## Open questions

- Is `AskUserQuestion` a tool workers should have at all? New evidence this shift: the
  planner runtime shows a real `PermissionRequest` for `AskUserQuestion` — so it costs a
  prompt like any other tool, and one per planning step rather than dozens.
- A permission answered mid-command still shows `blocked` until the *next* tool starts, so
  a single three-minute `pytest` lags. Only `PostToolUse` would close that, and mapping it
  would double every tool in the transcript. Left alone deliberately; revisit only if the
  lag is actually observed to mislead the Manager.
- Does the composite engine earn its keep now that Claude Code has dynamic workflows?
  Untested since `docs/architecture.md` argued yes.
- Read-only is a tool policy, not a sandbox (workers keep Bash). Does that matter in
  practice?
- How much of the 2500-line `SessionManager` is load-bearing after the recent fixes?

## Research notes

None yet. Adjacent systems worth a shift: Claude Code's own agent teams and dynamic
workflows, Agent Deck, AWS agent/CLI orchestrators. Produce hypotheses to test here, not
features to copy.
