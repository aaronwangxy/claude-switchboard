# Switchboard

I already get a lot done by running many independent Claude Code sessions at once.
Switchboard automates *my* side of that — choosing the workflow, feeding one session's
output to the next, noticing which one needs me — without taking away my ability to drop
into any session myself.

You give one Manager a request. It decides what sessions to run, hands each one's output to
the next, and tells you — from stored evidence, not from an opinion — when the work is
actually done. Every worker stays an ordinary Claude session you can walk into.

```
+------------------------------+----------------------------------------+
| Manager status + goal input  | Selected session                       |
|                              |   workflow / lifecycle / ownership      |
+------------------------------+   worktree / lineage / run step         |
| Sessions + attention queue   |   evidence ✓ ✓                          |
|  ! needs you  ● working  ✓   |   Enter → the exact native Claude       |
+------------------------------+----------------------------------------+
```

## Why this exists

The bottleneck was never the agents. It was me. For each piece of work I would open a fresh
session, give it the task, pick the ritual, watch it, answer its questions, and keep track
of which of ten sessions was working, blocked, finished, or waiting on me. That is fine for
three sessions and tedious at ten.

So the job to automate is the *operator's*, not the agent's — and the ritual being operated
is this one, across multiple long-running sessions:

```
goal → plan → approval → implementation → verification → independent review → iteration → done
```

with one property that turns out to be hard: **I have to be able to walk into any live
session, work in it normally, walk out, and have the larger workflow carry on safely.**

That combination is what was missing.

- **Native agents** are excellent at the individual-agent part — the loop, the tools, the
  subagents, the permission UI. But the multi-stage state lives in a conversation. Which
  stage a change is at, whether the plan was approved, whether the verification still
  applies to the current commit: all of it is something a model remembers, and a context
  that can be compacted is not a place to keep an approval gate.
- **Session managers** can run many agents at once, but they generally manage *sessions*.
  The dependency between stages, the evidence that a stage actually passed, the approval
  that unlocks the next one, and the Git lineage that says which tree the reviewer should be
  looking at are not first-class objects.
- **Orchestrators** coordinate tasks, but this particular combination — persistent *native*
  sessions you can take over mid-flight, deterministic engineering-workflow semantics,
  human takeover with reconciliation afterwards, and durable Git-aware state — is the shape
  I could not assemble from what I had.

So Switchboard is deliberately thin. Claude Code keeps everything it is already good at.
Switchboard owns the layer above: understanding a casual reference to work in flight,
resolving it against the durable relationships between jobs, repositories, branches,
worktrees and sessions, choosing the ritual you prefer, and holding the contracts and
evidence that decide whether a change is actually finished.

> This section is a description of the gap I hit, not a competitive claim. A detailed
> comparison against current tools has not been done yet.

## Goal, criteria, evidence

What makes delegation reliable is not a better prompt; it is an executable contract around
the agent. Every request has a goal, so every workflow shares one spine:

```
goal  ->  acceptance criteria  ->  evidence
```

| Artifact | Question | Typically produced by |
| --- | --- | --- |
| `goal` | What is this trying to achieve, and what would establish it? | planner, investigator |
| `implementation_contract` | What shape should the solution take? | planner |
| `findings` | What is actually going on, and what is the evidence? | investigator |
| `verification` | What proof was observed, at which commit? | verifier |
| `review` | Does it hold up to a fresh independent reading? | reviewer |

These are structured artifacts, not prose in a transcript, and the application enforces
them. Implementation cannot start without an approved plan. A fix cannot start before
something was actually diagnosed. A code change deterministically invalidates the
verification and review that no longer apply.

**Done is defined by the workflow, not by a fixed checklist.** A job is complete when
everything its workflow's unconditional steps promised exists, still applies to the current
commit, and passes the check its type carries — so `complete-ticket` demands a plan,
criteria, verification and review, `rebase` demands a clean tree and fresh checks, and
`investigate` demands an evidenced answer. `sb workflows` prints each one's definition of
done; `sb workflows validate` checks them before you rely on one.

## How it works

Say what you want into one Manager input. It picks a workflow — matching what you want
established against what each workflow would prove — and runs it across independent
sessions.

```
you → Manager Claude → pick the workflow whose definition of done matches
                                                       ↓
                             independent Claude sessions, one per stage
                                                       ↓
                             evidence stored, next session handed it
```

Firefighting, for instance, is an ordinary workflow rather than a special case:
`diagnose-and-fix` runs one session to diagnose, hands its stored findings to a second that
fixes them, then a third verifies and a fourth reviews. When work genuinely belongs in
separate jobs, Manager links them so evidence still travels and the parent stays open until
its children close.

For engineers who want to know how the interesting parts are actually achieved:

- **Manager and workers are real native Claude Code processes.** Not an SDK loop, not a
  reimplementation. Workers inherit your normal user, managed/company, project, and
  project-local configuration in their repository or worktree.
- **tmux provides the persistent terminals.** One dedicated server, one session per runtime
  generation, so a process outlives the board and keeps an independent attach target.
- **The Manager gets a constrained Switchboard MCP** over a mode-0600, generation-specific
  Unix socket into the board's own `SessionManager`. Every call revalidates the current
  manager identity and generation.
- **Workers get no orchestration authority at all** — never the manager's MCP config,
  socket, or launch arguments. It is structurally unreachable, not merely discouraged.
- **Claude's lifecycle hooks supply the semantics** — eleven of them, including
  `SessionStart`, `UserPromptSubmit`, `PermissionRequest` and `Stop`. Terminal contents are
  never scraped to guess what an agent is doing. A turn carries a durable provenance token,
  so a prompt you type by hand cannot complete a managed turn or advance a workflow.
- **SQLite holds the orchestration state,** independently of any Claude history. Losing a
  transcript cannot change whether a plan was approved.
- **Git worktrees isolate writable work.** One per writable worker, always under the managed
  root, never inside your repository. Read-only reviewers and verifiers observe rather than
  own.
- **Each job has one authoritative worktree,** and that lineage is the only thing reviewers,
  verifiers, freshness, and the completion gate inspect.
- **Native Claude features are configured, not reimplemented.** Model, effort, permission
  mode and the session's display name are passed through, so a worker shows up in your own
  `/resume` picker and `claude agents --json` like any session you started yourself.
- **Interruptions are minimised, not bypassed.** You vouch for a repository once and
  Switchboard answers the per-worktree trust dialog under four Python guards; writable
  workers run with `acceptEdits` in their own worktree so they do not stop on every write.
- **Pressing Enter puts you in the exact same process.** No `--resume`, no replacement
  process, no interrupted turn. Ownership becomes yours, Switchboard refuses to send while
  you are there, and the run pauses in a resumable state.
- **Restarting the board adopts surviving sessions** by exact runtime id and generation,
  rather than recreating them — and refuses to adopt anything that does not match.

Details live in [`docs/architecture.md`](docs/architecture.md) and
[`docs/runtime.md`](docs/runtime.md).

## Install

Requires `git`, `tmux`, and the `claude` CLI on your `PATH`;
[uv](https://docs.astral.sh/uv/) supplies the Python (3.12+).

```bash
uv tool install git+https://github.com/aaronwangxy/claude-switchboard
```

That is the whole install — `sb` lands on your `PATH` with the built-in workflows inside the
package. Re-run with `--reinstall` to pick up a newer `main`.

From a checkout, to work on Switchboard itself:

```bash
uv tool install --editable .    # `sb` runs this checkout; source edits are live
uvx --from . sb                 # or just try it without installing
```

## Run

```bash
sb                          # launch the board (or: sb claude)
sb --register /path/repo    # register a repository at startup
sb workflows                # what this installation can route to
sb config                   # effective configuration and its paths
SB_BACKEND=scripted sb      # offline demo: no model is called
```

## Using it

Press `?` for the key bindings. The important interaction model:

- **Manager** is the top-left pane and the one input. Paste a ticket, ask a question, give
  an instruction. It is a router and a status oracle; it never writes code.
- **Sessions** lists Manager and every worker, attention-first: `!` needs you, `●` working,
  `✓` idle or done. `Ctrl+J` / `Ctrl+K` step through them.
- **The detail pane** shows durable orchestration state for the selected session — workflow,
  lifecycle, ownership, worktree and branch, which run step it is on, and which contracts and
  evidence exist.
- **Enter** (or `Ctrl+E`) opens the exact live Claude session. Work in it normally. When you
  leave, confirm Claude's composer is empty so Switchboard can drive it again.
- **Approvals and attention.** A workflow that needs you appears in the queue rather than
  waiting silently. Approving a plan in your own words unblocks its run; a paused run stays
  paused until you say "resume the run", because you may have edited by hand and only you
  know whether the ritual should carry on.

Workflows are YAML and adding one requires no change to Switchboard:

```yaml
name: post-rebase-verify
description: Re-verify a change after rebasing it, because the old evidence no longer holds.
steps:
  - workflow: rebase-stack
  - workflow: full-verify
  - workflow: smoke-test
```

Drop that in `~/.switchboard/workflows/` for yourself, or in a repository's
`.switchboard/workflows/` so the convention travels with the clone. Built-in names are
reserved, so a file inside a repository cannot strip the prerequisites that keep
implementation from running without an approved plan. `mine-workflows` reads Switchboard's
own record of what you keep running and *proposes* workflows for rituals you assemble by
hand; a proposal changes nothing until you accept it.

## Safety

The application, not the agent, owns every destructive path. Git is invoked as an argument
array, never through a shell. Worktrees live outside your repository and only under the
managed root. Cleanup requires explicit confirmation and refuses to remove a worktree holding
uncommitted or unmerged work. Branches are never deleted, and nothing pushes, force-pushes,
or merges. The full list is in
[`docs/architecture.md`](docs/architecture.md#safety-invariants).

## Configuration

Nothing is required. `sb config` shows the effective settings and the main paths;
[`docs/configuration.md`](docs/configuration.md) explains them, and
[`config.example.yaml`](config.example.yaml) is a starting point.

## Status and limitations

A working prototype, built for personal use, and dogfooded on real work — see
[`docs/dogfood-report.md`](docs/dogfood-report.md) for what that found.

The ones worth knowing before you try it:

- **Answering a question the agent itself asked taints the attempt.** Entering a session
  during a managed turn is what keeps a hand-edited attempt from counting as the ritual's
  output — but it applies even when you entered only to answer the agent, so work can be
  discarded. This is the sharpest edge in the product today.
- **Native prompts dominate the loop.** Every new repository and worktree costs a Claude
  trust prompt, and a writable worker's first `Edit` and `Bash` cost permission prompts.
- **Nothing reclaims orphaned runtimes.** Sessions deliberately survive the board quitting so
  the next one can adopt them, but `sb` cannot yet list or stop them.
- **Read-only is a tool policy, not a sandbox.** Read-only workers keep `Bash`, because
  reviewers and verifiers need it.
- **Single-process, single-user.** Two boards against one data directory could race.

The complete list, with symptoms and workarounds, is in
[`docs/troubleshooting.md`](docs/troubleshooting.md).

## Documentation

| | |
| --- | --- |
| [architecture.md](docs/architecture.md) | The system, its boundaries, and the safety invariants |
| [runtime.md](docs/runtime.md) | tmux, runtime generations, hooks, entry, recovery |
| [workflows.md](docs/workflows.md) | Workflows, contracts, evidence, composite runs |
| [manager.md](docs/manager.md) | The native Manager and its constrained MCP |
| [configuration.md](docs/configuration.md) | Every setting, path, and environment variable |
| [development.md](docs/development.md) | Setup, the four test tiers, commit expectations |
| [troubleshooting.md](docs/troubleshooting.md) | Symptoms, workarounds, known limitations |

Those seven describe the system as it is. Two more are dated records kept deliberately
unedited, so they describe moments rather than the current build:
[project-evolution.md](docs/project-evolution.md) for how the architecture got here and how
it was built, and [dogfood-report.md](docs/dogfood-report.md) for two sessions of using it
adversarially on real work.
