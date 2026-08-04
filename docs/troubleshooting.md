# Troubleshooting and known limitations

Most of what follows was found by using Switchboard on real work rather than by reading the
code. The full field record is in [dogfood-report.md](dogfood-report.md).

## A worker sits in `starting` forever

Almost always native Claude's own first-use prompt: workspace trust for a repository or
worktree it has not seen, or an expired login. The board says so and names the key. Press
`Ctrl+E` (or Enter) on that session, answer Claude's prompt, clear the composer, and hand
control back with `y`. The workflow prompt that was waiting is delivered on return.

This costs a trust prompt for every new repository *and* every new worktree, which in
practice is the single biggest source of interruption in a normal session.

## "Cannot enter that session: ... open a separate terminal"

You are inside a tmux client belonging to a different tmux server. Switchboard refuses to
nest tmux inside tmux. Open a separate terminal — one not already attached to another tmux
server — and enter from there. A client on Switchboard's own server switches directly.

## Entering a session discarded the work it was doing

This is the sharpest edge in the product. A managed turn that a human touches is durably
tainted, so its result cannot advance a workflow — that is the safety property that keeps a
hand-edited attempt from silently counting as the ritual's output. But it applies even when
you entered only to answer a question the agent itself asked, so a good plan can be produced
and then discarded.

Workaround today: let a turn finish before entering when you can, and use `resume_run` to
replay the tainted step from the durable contracts. It does not consume an iteration.

The real fix is to integrate with Claude's own question and permission UI rather than route
around it, so that answering an agent is not the same act as taking a session over. That is
a substantial piece of work and is not done.

## "You are attached to X. Talk to it in that session, or leave it before sending"

Expected. While you own a session's input lane, Switchboard will not become a second writer.
Leave the session and confirm the composer is empty.

## A run is paused and will not continue on its own

Also expected, after any human intervention. You may have edited by hand, so whether the
ritual should carry on is your call. Tell the Manager to resume the run.

## The board says nothing needs me, but a job is not moving

Check the selected job's detail pane for `active run … awaiting_approval`. The status summary
describes an approval-gated run as an "idle incomplete job", which is imprecise wording; the
attention queue itself is correct.

## Stopping everything

Quitting the board deliberately leaves native runtimes alive so the next controller can adopt
them. When you want the opposite — the board, the manager, and every worker gone — `sb kill`
stops all three:

```bash
sb kill        # prints what it found, then asks
sb kill -y     # no confirmation
```

It reads no orchestration state and writes none, so it still works when the board is wedged
enough that its own UI cannot quit, and it touches no worktree, branch, or database row.

It is not a restart. Quitting the board leaves runtimes alive so the next board adopts the
same live sessions; a killed runtime cannot be adopted, so the next `sb` reconstructs it as a
*fresh* native session from durable state. The job, its artifacts and its transcript survive —
the conversation each worker was holding does not, and a composite run whose step was in
flight is paused for reconciliation rather than resent. Prefer quitting the board; kill when
you want the processes gone.

Everything it stops is scoped to one data directory, which it prints. A board started under a
different `SB_HOME` is out of reach — boards are identified by which database they hold open
and runtimes by the `--settings` path they were launched with, because two homes are otherwise
indistinguishable in `ps`. Run it once per `SB_HOME` you have used.

Killing the tmux server alone is not equivalent. A native Claude process survives losing its
pane, so a bare `tmux kill-server` leaves the runtimes running; `sb kill` signals them by name
afterwards.

Isolated experiments under `SB_HOME` each get their own socket, and long `SB_HOME` paths get
one under `/private/tmp/switchboard-tmux-<digest>.sock` because macOS limits Unix socket path
length.

## `claude.executable ... is not executable`

A shell alias or shell function cannot be launched as a process. Point `claude.executable` at
a real executable wrapper.

## Manager reports a launch-configuration mismatch and refuses

A live Manager process exists whose launch fingerprint does not match this controller's
configuration — usually because a setting that feeds the fingerprint changed while it was
running. Switchboard refuses to mint a peer generation. Finish or stop that Manager session
explicitly, then start the board again.

---

# Known limitations

Recorded rather than solved. Each is a real property of the current build.

- **Answering a prompt the agent itself raised taints the attempt and discards its work.**
  The most consequential item on this list; see above.
- **No first-class runtime listing or reclamation.** `sb` exposes `claude`, `workflows`, and
  `config`; nothing sees or stops orphaned runtimes.
- **`Ctrl+Space` does not reach the application in a real terminal.** Use `Ctrl+J` /
  `Ctrl+K` to reach the session that needs you, or the attention ordering in the session
  list.
- **Read-only is a tool policy, not a sandbox.** Read-only workers are denied
  `Edit`/`Write`/`NotebookEdit`/`MultiEdit` and run in native plan mode, but they keep
  `Bash` because reviewers and verifiers need it. A worker that deliberately wrote through
  `Bash` would not be stopped.
- **Verifiers dirty the worktree they inspect.** Read-only verifiers and reviewers run in the
  authoritative worktree so they see the change under review. Running a test suite there
  creates artefacts like `__pycache__`, which the ready-to-push gate then correctly reports
  as uncommitted changes. The gate is right; the ergonomics are not.
- **Blocked detection still relies on a marker.** A worker signals that it needs the user by
  ending its reply with `[NEEDS INPUT]` or `[NEEDS DECISION]`. Permission and elicitation
  prompts are detected properly through hooks, but a worker that asks a question in prose
  without the marker is recorded as idle.
- **The approval matcher is a heuristic over English.** It is tested against twenty-seven
  phrasings, eighteen of them adversarial, but it stays a lexical guard. The durable property
  it enforces is that the approval appeared in the user's own current turn.
- **The status summary's wording for approval-gated runs is imprecise.** See above.
- **A long Manager message can be clipped.** The Manager pane is fixed-height with no
  scrollback. Painting newest-first guarantees the beginning of the latest reply is visible,
  not its end.
- **The manager MCP socket path is visible in `ps`,** and the bridge's reconnect loop widens
  a pre-existing local socket-squat window on a multi-user host from a one-shot startup race
  into a recurring 30-second one. Accepted for a single-user tool.
- **`had_conflicts` is never set from a real rebase.** `classify_change` accepts it and
  `REBASE_WITH_CONFLICTS` / `CLEAN_REBASE` are implemented and unit-tested, but the rebase
  workflow does not parse Git's conflict state. A rebase that changes the tree is classified
  as an implementation edit, which invalidates a superset — the conservative direction.
- **No `git worktree` locking between board processes.** Two `sb` processes against one data
  directory could race. Single-process personal use is the documented operating path.
- **The Manager's reply is not streamed.** It appears when the turn finishes.
- **The manager model can still choose a worse route than the deterministic one.** The route
  proposal is advisory; only the safety invariants are mandatory. Refusals bound the damage;
  they do not guarantee an optimal route.

## Where the evidence is thinner than the tests suggest

Two behaviours have passing deterministic coverage but have never been replayed against a
live Claude: recovery across a pending plan approval, and the rapid Manager follow-up race.
Both are recorded here rather than counted as proven.

## Deliberate non-goals

Not built, by choice: a chat room for agents; unlimited agent fan-out; automatic merging,
force-pushing, or destructive cleanup; a multi-user or remote orchestration service; a plugin
marketplace; a generic workflow engine.
