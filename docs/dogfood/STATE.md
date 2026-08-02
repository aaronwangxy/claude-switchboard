# Dogfood state

Working memory for the autonomous dogfood loop (`.claude/loop.md`). Successive shifts read
and rewrite this file. It describes **now** — prune anything no longer true. It is not a
changelog; `git log` is. It is not the frozen field record; that is
[`../dogfood-report.md`](../dogfood-report.md).

Last shift: 2026-08-02.

## Active work — resume this first

**Scratchpad session feature, mid-flight in Switchboard itself.**

- Job: "Board keyboard shortcut to open an independent scratchpad Claude session",
  running `complete-ticket`, in an isolated `SB_HOME` under a scratch directory (the run
  may no longer be live; if the board is gone, the job is recoverable only from that home,
  otherwise restart the job on the real `SB_HOME`).
- Request: a board shortcut opening a plain Claude session tied to no job or workflow, for
  quick ad-hoc work, plus a help entry and tests.
- The planner reached these answers before being interrupted, and they match an
  independent read of the code:
  - **read-only, cwd = repository root, no worktree, no branch** — nothing to clean up,
    reuses the existing read-only worker path. (Known gap: read-only workers keep Bash.)
  - **Ctrl+T** — free at both app and Textual level, no substitution note needed.
- Not yet resolved in code, and worth deciding when implementing:
  - `_start_backend` sets `WORKING` unconditionally, so a session started with no prompt
    would sit on the board as "working" forever. A prompt-less scratchpad should be `IDLE`.
  - `_session_name` renders `"<title> <role>"`, so a title of "scratchpad" yields
    "scratchpad scratchpad".
  - `READ_ONLY_NOTE` tells the worker it is "working inside another worker's live
    worktree", which is already untrue for a `question` worker in the repository root and
    would be untrue for a scratchpad.
  - Which repository, when several are registered.

## Findings queue — ranked

1. **A planning step stalls on `AskUserQuestion` and the board cannot show or answer it.**
   Reproduced twice. `plan-feature` asks for decisions as prose ending `[NEEDS DECISION]`,
   but native Claude in plan mode reaches for its interactive question tool instead. The
   board shows only "Permission required for AskUserQuestion" — not the question, its
   options, or the recommendation — and the only way to answer is to enter the session.
   Two attention items are raised for the one prompt, and answering it in the pane does not
   resolve either; they linger until the next managed send. Options: deny the tool so the
   worker falls back to the prose the workflow already asks for; or surface the question on
   the board and answer it from there. Decide with evidence, this is the flagship path.
2. **Entering a session is impossible when the board runs inside tmux** — which is where a
   terminal tool usually runs. `TmuxView.argv` refuses to nest. It now hands over the exact
   `tmux -S … attach-session` command (commit below), but that workaround bypasses
   Switchboard's ownership accounting entirely: the human turn is invisible to the runtime
   owner, so attention is never resolved and `human_intervened` is never set. A real fix
   probably claims human ownership before printing the command.
3. **Worker-status truth after a human answers a native dialog.** Follows from 1 and 2 —
   a session that a person unblocked by hand keeps a stale `blocked` status and stale
   attention on the board.

## Landed this shift

Three fixes, all found by using the product rather than reading it:

- `fix(runtime): never write a pre-launch snapshot over a session's own SessionStart` —
  the supervisor read the runtime row before launching and wrote it back afterwards to
  record the tmux pane, erasing a `SessionStart` that landed in between. SessionStart fires
  once, so a healthy session sat at an empty composer while the controller waited out its
  timeouts, declared it blocked on a startup prompt that did not exist, and never delivered
  the prompt it was started for. Every persist after the process exists is now a
  read-modify-write. This was the reason the first dogfood run stalled at step 1.
- `fix(manager): retire a Manager the current configuration cannot adopt` — editing the
  Manager prompt while its session runs changes the fingerprint, and the board then refused
  every message *and* Ctrl+E with the same refusal, escapable only by the undocumented
  phrase "fresh manager". Mismatched manager-owned processes are now retired and replaced;
  one the user owns is still never taken from them.
- `fix(manager): reconcile a turn that outran the controller's wait` — a turn finishing
  after the 180s wait left the runtime in `turn_complete`, and every later message answered
  "waiting on a startup prompt — press Ctrl+E" forever.

Plus the Ctrl+E refusal now carries the command that does work.

## Rejected

- **Giving the scratchpad its own worktree.** Isolation is real, but a throwaway session
  should not leave a branch and a cleanup ritual behind. Read-only in the repository root
  costs nothing to undo.
- **Leaving a mismatched Manager alive and only improving the error text.** Tried first;
  the board is still unusable, and the remedy is a phrase nobody would guess.

## Open questions

- Is `AskUserQuestion` a tool workers should have at all? The board is meant to be where a
  decision surfaces, but denying a native primitive to reimplement it is exactly what
  `CLAUDE.md` warns against. Wants evidence, not taste.
- Does the composite engine earn its keep now that Claude Code has dynamic workflows?
  `docs/architecture.md` argues yes (durability, human gates, real sessions). Unretested
  since it was written.
- Read-only is a tool policy, not a sandbox (workers keep Bash). Does that matter in
  practice, or only on paper?
- How much of the 2500-line `SessionManager` is load-bearing after the fixes above?

## Research notes

None yet. Adjacent systems worth a shift: Claude Code's own agent teams and dynamic
workflows, Agent Deck, AWS agent/CLI orchestrators. Produce hypotheses to test here, not
features to copy.
