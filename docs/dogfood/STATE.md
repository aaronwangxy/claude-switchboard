# Dogfood state

Working memory for the autonomous dogfood loop (`.claude/loop.md`). Successive shifts read
and rewrite this file. It describes **now** — prune anything no longer true. It is not a
changelog; `git log` is. It is not the frozen field record; that is
[`../dogfood-report.md`](../dogfood-report.md).

Last shift: 2026-08-03 (midday).

## Active work

**None. Nothing is running.** Every tmux server is gone — no default-socket board, no
`/private/tmp/switchboard-tmux-*.sock`. Five orphan native `claude` processes survive with
no session to belong to.

The scratchpad experiment that three shifts carried is over: its board died with its tmux
server, so `complete-ticket` step 2/8 cannot be resumed. Its commit is safe — branch
`sb/board-keyboard-shortcut-to-open--95fa5788` at `7272ce4` is a ref in this repository —
but its **uncommitted** work is not:

```
/private/tmp/claude-501/-Users-aaron-dev-claude-switchboard/726099bb-.../scratchpad/home/
  worktrees/claude-switchboard/board-keyboard-shortcut-to-open--implementer-95fa5788
  M src/switchboard/core/session_manager.py
  ?? tests/integration/test_scratchpad.py
```

Under `/private/tmp`, so macOS will reap it. Rescue it or let it go, deliberately; a
worktree with uncommitted work is not something a shift removes.

The real `SB_HOME` still holds the older dead attempt: one disconnected planner, one idle
planner, `complete-ticket` paused at step 0, one unhandled attention item. Retire it from
the board when convenient.

## Landed this shift

`854a4ac docs: say how a permission rule matches a worker's command`.

The shift before this one left an uncommitted change in the working tree — a
`COMMAND_SHAPE_NOTE` added to every writable worker's system prompt, plus a policy-version
bump — and left `0e1c30e` unrecorded here. I measured the note's premise before shipping
it. **The premise was false, and the note would have made workers slower.** Reverted; the
true half of it is now two paragraphs of documentation instead.

### What a permission rule actually matches

Measured against claude 2.1.220 with real `claude -p` runs over a `--settings` overlay,
`--output-format stream-json` so the reason is the CLI's own text, not a paraphrase:

- **A chain is split and matched part by part.** `git status --short && chmod 644 file.txt`
  runs unattended with rules for both; with a rule for `git status` only, it asks once —
  *"This Bash command contains multiple operations. The following part requires approval:
  chmod 644 file.txt"*.
- Therefore **"one command per Bash call" is a regression**: a chain of five with one
  unmatched part costs one prompt, and splitting it costs one per unmatched part. The
  reverted note would have taught that to every writable worker.
- **Heredocs analyse fine**: `git commit -q -F - <<'EOF'`, `python3 - <<'EOF'`, and the
  `git commit -m "$(cat <<'EOF' … EOF)"` form Claude reaches for by default all ran under
  `Bash(git commit:*)` / `Bash(python3:*)`.
- **Command substitution over state is what actually defeats a rule.** `echo "$(cat
  msg.txt)"` → *"Contains shell syntax (string) that cannot be statically analyzed"*, with
  `Bash(cat:*)` allowed as well as without it. Backticks likewise. Substitution that
  resolves to a constant (the heredoc form above) is fine.

So STATE's old claim that `complete-ticket`'s commit step must stop using a heredoc is
**wrong and retired**. No workflow YAML instructs a heredoc anyway. Nothing in Python or
in the built-ins needs to change for `writable_worker_allow` to work.

Probe scripts are in `/tmp/sb-probe*/`; they will not survive, but each case above is one
scratch repo, one settings file, one `claude -p`.

## Last step-back

**2026-08-03. Verdict: one real root cause, currently with no surviving instance, and no
structural guard against the next one.**

Four fixes across recent shifts are one defect: `0b92ea0` (pre-launch snapshot over
SessionStart), `fd1ee0c` (snapshot written back across a tmux round trip — its own message
says *"the same defect 0b92ea0 fixed on the launch path, in the two methods it did not
reach"*), `e572f52` (a turn that outran the controller's wait), `9bd0b68` (a worker running
again still marked blocked on its prompt).

**The cause: a runtime row has two writers.** Claude's hooks run in Claude's own process
and commit throughout a call; Switchboard's methods do whole-row read-modify-write. Any
method that reads a row, spends a subprocess or a turn, then saves that object erases
whatever the hook wrote meanwhile — and a hook fires once.

Predicted: more instances at the other `save_runtime` sites. **Checked, and the prediction
fails** — 14 sites across `session_manager.py`, `native_claude.py` and `supervisor.py`, and
every one either re-reads immediately before writing (`_start_backend`'s failure branch,
`_finish_launch`, attach, `_bind_target`, `_record`) or writes a field it read with no slow
call in between. The sweep is complete.

What is not fixed is that this is **per-site discipline, not an invariant**. Nothing stops
the next method from reintroducing it; `CLAUDE.md`'s invariant list does not cover it, and
the only thing carrying the rule is a comment repeated at five sites. The structural fix is
to stop passing whole rows to the store: a field-level `update_runtime(id, **fields)` that
reads and writes inside one transaction, so a caller can only overwrite what it names. 14
call sites, each needing a judgement about which fields it owns.

I did not start it: this shift had already landed its work, and a half-finished refactor of
the hub plus the storage layer is worse than a written-down cause. **It is the strongest
candidate for the next shift.**

## Still needs your decision

- **What to put in `writable_worker_allow`.** Unchanged and still yours: the mechanism
  shipped in `debb2a0`, empty by default. The commands `CLAUDE.md` documents are the
  honest candidates. An agent should not decide which commands it may run unattended.
  Now known: whatever you write, chains are fine as long as every part is covered.
- **`permissions.writable_worker` off `acceptEdits`** is still available and still a real
  posture change. The allow list is the narrower move; take it first.

## Recorded late: 0e1c30e

The shift that landed it never wrote it up. `permission_summary` names the call a worker is
blocked on — a command, a path, a URL — because the stored reason had been *"Permission
required for Bash."* for every Bash prompt in a job. Eight identical strings for eight
different commands, replayed from a real run. Without it neither the Manager nor the user
can tell routine verification from something that deserves a person, which is also what
made `writable_worker_allow` impossible to fill in honestly.

## What this harness would not let me do

- **`git push origin main` — refused by the harness classifier.** So this shift's four
  commits sit on local `main`, unpushed, with the suite green (458 passed, ruff and mypy
  clean). Push them yourself, or expect the next shift to find them here.
- Spawning subagents, so no independent review of anything a shift lands.
- `tmux send-keys` into a worker pane — allowed early in a shift, blocked later. Driving an
  implementer to completion by hand is not something a shift can rely on.
- Committing a permissions allow list *for this repository*, or building a prompt
  auto-answerer. Shipping the mechanism with an empty default is a product capability;
  filling it in is your call.

## Rejected

- **The `COMMAND_SHAPE_NOTE` prompt paragraph.** False premise, and it would have cost
  prompts rather than saved them. The residue that is true — command substitution over
  state is never matchable — is too rare in a worker's normal flow to spend system-prompt
  text and a policy-version bump on; it already fails loudly and legibly.
- **Seeding `writable_worker_allow` with defaults.** The agent granting itself permissions.
- **Killing the five orphan `claude` processes**, and **removing the scratchpad worktree**
  with uncommitted work in it. Your sessions, your call.

## Open questions

- Should Switchboard trust a worker's worktree the way it trusts a repository? Invariant 15
  answers a *trust dialog* in a pane; it evidently does not leave a durable trust entry for
  the worktree, since none exists for any. If it did, repository-level settings would start
  applying to workers — which is either the right unification or a quiet widening.
- Nothing reclaims a native session when its board dies, and nothing reclaims a worktree
  under an ephemeral `SB_HOME`. Two dead experiments now sit in durable state that only a
  human can retire. Should a board adopt or bury what it finds orphaned at startup?
- Is `AskUserQuestion` a tool workers should have at all? It costs a real
  `PermissionRequest` like any other tool, but one per planning step rather than dozens.
- A permission answered mid-command still shows `blocked` until the *next* tool starts, so
  a single three-minute `pytest` lags. Only `PostToolUse` would close that. Left alone;
  revisit only if the lag is observed to mislead the Manager.
- Does the composite engine earn its keep now that Claude Code has dynamic workflows?
  Untested since `docs/architecture.md` argued yes.
- Read-only is a tool policy, not a sandbox (workers keep Bash). Does that matter in
  practice?
- How much of the 2500-line `SessionManager` is load-bearing after the recent fixes?

## Research notes

None yet. Adjacent systems worth a shift: Claude Code's own agent teams and dynamic
workflows, Agent Deck, AWS agent/CLI orchestrators. Produce hypotheses to test here, not
features to copy.
