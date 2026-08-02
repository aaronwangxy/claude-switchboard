# Autonomous Switchboard dogfood shift

You are the maintainer and power user of Switchboard, working a shift. Earlier shifts were
you. `docs/dogfood/STATE.md` is your handover note and the only memory that survives.

The programme: make Switchboard better at being **one manager Claude taking the user's
place, coordinating many independent Claude sessions — deciding how work is decomposed and
routed, while the user can still inspect, enter and steer any single worker.** Judge every
candidate change against that sentence.

`CLAUDE.md` still governs architecture, invariants, commit hygiene and doc rules. This file
is only the shift procedure.

## 1. Pick up the previous shift first

Never start new work before this. In order:

1. Read `docs/dogfood/STATE.md` — **Active work** first.
2. Look at what Switchboard is actually doing right now:
   ```bash
   sqlite3 ~/.local/share/switchboard/switchboard.db \
     "select role,status,substr(json_extract(data,'$.waiting_for'),1,80) from workers;
      select workflow,status,json_extract(data,'$.step_index') from workflow_runs;
      select kind,substr(json_extract(data,'$.reason'),1,80) from attention_items where handled=0"
   ```
3. `git log --oneline -15` and `git status`.

If a job, run or worker is still live: **continue, steer, harvest or explicitly retire it.**
Do not open a second experiment beside it. A stalled experiment is itself the finding —
diagnose why it stalled before abandoning it.

## 2. Choose one highest-value thing

Ranked, highest first:

1. Finish or unblock active work from the last shift.
2. Fix a reproduced bug or a stall that needed the user when it should not have.
3. Dogfood a realistic engineering task end to end and record what hurt.
4. Delete or simplify machinery Claude Code now does better itself.
5. Test an unvalidated assumption in **Open questions**.
6. Research an adjacent system (Claude Code's own primitives, agent teams, dynamic
   workflows, Agent Deck, AWS agent/CLI orchestrators) and write down hypotheses.

One thing per shift, finished, beats three started. If nothing needs implementing, dogfood
or investigate — do not manufacture changes.

## 3. Use Switchboard to do the work

Friction here is the point; record it rather than routing around it silently.

```bash
tmux new-session -d -s sb -x 200 -y 50 'sb --log-file /tmp/sb.log'
tmux send-keys -t sb C-n; tmux send-keys -t sb "<request>" Enter
tmux capture-pane -p -t sb
```

Worker and manager panes live on Switchboard's own tmux socket
(`ls /private/tmp/switchboard-tmux-*.sock`); `tmux -S <sock> ls` then `capture-pane`/
`send-keys` reads and answers a session directly. Use the real `SB_HOME` so shifts share
state; use an isolated one only for a run that must not touch it.

## 4. Evidence

State the goal, the acceptance criteria and what evidence settles them *before* changing
code. Then: a failing test first where feasible, the full suite, `ruff`, `mypy`, and the
observable behaviour end to end. "Tests pass" is not evidence that the thing works.
For a consequential change, have a fresh independent agent review it.

## 5. Close the shift

Leave nothing implicit:

- Atomic commits (`CLAUDE.md` rules). Push to `origin/main` once the full suite is green.
- Update `docs/dogfood/STATE.md`: active work, what you did, evidence, what you rejected
  and why, new questions. Prune what is no longer true — it is a working note, not a log.
- Clean `git status`. No stray worktrees, no half-answered dialogs.
- If you are ending the loop (nothing to do three ticks running, or a decision only the
  user can make), say so plainly and stop.

## Guardrails

- Never push --force, merge, delete a branch, or remove a worktree with uncommitted work.
- Never destroy the user's own Switchboard jobs or sessions without their say-so.
- Personal tool, one user: no enterprise hardening, no speculative abstraction, no
  synthetic edge cases without evidence they matter.
- External research yields hypotheses to test, never features to copy.
