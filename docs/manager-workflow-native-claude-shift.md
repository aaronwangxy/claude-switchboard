# Claude Switchboard — Manager / Workflow / Native Claude Shift

## Objective

Reorient Claude Switchboard around its intended product model:

> **Switchboard replaces me as the human operator of many independent Claude Code sessions, while preserving my ability to inspect and steer any individual session.**

Today, when I receive work, I manually:

1. Open a fresh independent Claude Code session.
2. Give it the task.
3. Choose or execute the appropriate workflow.
4. Monitor it.
5. Answer questions or steer it.
6. Repeat this across many simultaneous pieces of work.
7. Keep track of what is working, blocked, finished, needs review, or needs me.

This works for a few sessions but becomes annoying with 10+ independent Claudes.

Switchboard should automate **my role as the operator**.

The target interaction is:

```text
TODAY

me
├─ Claude A → workflow
├─ Claude B → workflow
├─ Claude C → workflow
└─ Claude D → workflow

I supervise every Claude manually.
```

```text
DESIRED

me
└─ Manager
   ├─ Claude A → workflow
   ├─ Claude B → workflow
   ├─ Claude C → workflow
   └─ Claude D → workflow

Manager supervises the Claudes on my behalf.
```

The goal is **not** to eliminate independent Claude sessions.

The goal is to eliminate the need for me to manually orchestrate all of them.

Every worker should remain a real, inspectable Claude Code session. At any time I should be able to:

- see which Claude owns a piece of work
- inspect its conversation/output
- attach to the actual session
- answer a question
- steer or correct it
- leave it and return to Manager

---

# Core Architecture

There are three primary layers:

1. **Manager** — conversational entrypoint and proxy operator
2. **Workflows** — reusable orchestration recipes
3. **Workers** — ordinary independent Claude Code sessions

The governing ownership rule is:

> **Claude Code owns the agents. Switchboard owns the workflow they are participating in.**

---

# 1. Manager

Manager should be the primary way I interact with Switchboard.

It should behave like a proxy/copy of me that has reliable knowledge of Switchboard's durable work-in-flight state.

I should be able to say:

- implement ticket ENG-123
- rebase auth onto main
- investigate this startup race
- review the payments work
- continue the ticket from yesterday
- what's blocked?
- which workers need me?
- why isn't auth ready?
- tell the verifier to focus on the startup race
- which jobs are ready to land?

Manager may:

- understand natural-language intent
- resolve existing jobs/workers/runs
- choose an appropriate workflow
- create/start jobs and workflow runs
- launch independent Claude workers
- route follow-ups
- monitor progress
- surface blockers and attention
- request human approvals
- summarize current state
- recover/resume work

Manager is **not authoritative merely because it is an LLM**.

Deterministic Switchboard state remains authoritative.

Manager should not silently become the implementation worker or directly edit repositories instead of launching/managing workers.

The normal interaction is:

```text
me
→ Manager
→ workflow
→ independent Claude session(s)
```

---

# 2. Workflows

Workflows encode the repetitive procedures I currently execute manually after opening a Claude.

The existing implementation/contract flow is **one workflow**, not the architecture itself.

Examples of peer workflows:

- `implement-contract`
- `rebase`
- `investigate`
- `review-only`
- `fix-ci`
- `one-shot`

Example:

```text
implement-contract

plan
→ optional human approval
→ implement
→ verify
→ review
→ evidence/freshness
→ ready
```

Another:

```text
rebase

inspect
→ rebase
→ resolve conflicts if necessary
→ verify resulting history/tests
→ ready
```

Another:

```text
investigate

spawn investigator(s)
→ gather findings
→ synthesize
→ report
```

A workflow may use contracts, gates, evidence, repetition, or multiple workers where useful.

Not every workflow needs the full implementation-contract machinery.

Contracts are **workflow primitives**, not a special architectural layer above workflows.

Preserve the valuable existing concepts:

- implementation contracts
- behavioral/acceptance contracts
- evidence contracts
- human approval gates
- independent verification
- review
- Git-based evidence freshness
- authoritative implementation lineage
- deterministic readiness/completion
- bounded repetition/recovery

---

# Intended Simple Behavior

If I tell Manager:

> Implement ENG-123

I should not need to think:

> I need a planner Claude, then an implementer Claude, then a verifier Claude.

Manager should decide that from the workflow.

Conceptually:

```text
me
"Implement ENG-123"
      ↓
Manager
      ↓
choose implement-contract
      ↓
create/select authoritative worktree
      ↓
launch independent Claude worker(s)
      ↓
run workflow
      ↓
request my approval only if required
      ↓
launch verifier/reviewer if required
      ↓
record/check evidence
      ↓
invalidate stale evidence if implementation changes
      ↓
tell me when genuinely ready
```

Likewise:

> Rebase auth onto main.

should select the rebase workflow rather than forcing implementation-contract semantics onto the task.

---

# 3. Workers

A worker should be as close as practical to:

> **Claude Code exactly as I would have launched it myself.**

Switchboard should provide only the context needed:

- isolated/authoritative worktree where appropriate
- task
- workflow/role context
- required artifact/contracts
- appropriate native Claude configuration

Then let Claude be Claude.

Claude should own as much worker-local behavior as possible:

- implementation reasoning
- local planning
- `/goal`
- subagents
- Agent Teams
- Dynamic Workflows
- tools
- models / effort
- skills
- permissions
- sandboxing
- hooks
- memory
- local retry/execution loops

Switchboard generally should **not** model Claude's internal subagents.

Switchboard only needs durable semantic facts such as:

```text
worker X
belongs to job Y
is performing workflow step Z
has role R
uses authoritative worktree W
owes artifact A
must satisfy contract B
has evidence C
is working / blocked / done
```

If a worker internally launches ten subagents, that is Claude's concern unless their existence affects a durable Switchboard contract.

---

# Native Claude First

Apply this rule aggressively:

> **If modern Claude Code already provides a capability reliably, prefer using the native capability instead of maintaining a Switchboard reimplementation, unless Switchboard needs stronger durable cross-session semantics.**

Investigate and use where appropriate:

- `/goal`
- Stop / TaskCompleted / other hooks
- native permissions
- native OS sandbox
- model / effort configuration
- skills
- native/session agents where semantics fit
- subagents
- Agent Teams
- Dynamic Workflows
- native memory
- background/persistent sessions
- Agent View
- `claude agents --json`
- attach/log/stop/respawn
- native worktree behavior

Claude-local completion is not automatically Switchboard workflow completion.

For example:

- `/goal` may keep one worker working until its local task appears complete.
- Switchboard may still require independent verification and fresh evidence before advancing the workflow.

Do not replace stronger Switchboard invariants with weaker model assertions just to delete code.

---

# Native Session Supervision

Investigate whether modern Claude background sessions / Agent View can replace some custom tmux/process supervision.

Potential native primitives:

- `claude --bg`
- `claude agents --json`
- attach
- logs
- stop
- respawn

Use them only through supported interfaces and only where they provide equivalent capability.

Do not:

- depend on undocumented Claude internals
- prematurely remove the existing backend
- lose reliable programmatic managed follow-ups/replies
- weaken restart/recovery guarantees

Preserve the backend abstraction so native supervision can evolve independently.

If current Claude cannot cleanly provide an essential requirement, keep the existing mechanism and document why.

---

# Worktree Ownership

Keep Switchboard ownership of authoritative workflow/implementation lineage.

Preferred model:

```text
Switchboard creates/selects authoritative linked worktree
→ launches Claude inside it
→ Claude operates normally
```

Claude-native worktrees are useful for process isolation.

Switchboard's worktree additionally means:

> **This is the implementation that this workflow, verifier, reviewer, contracts, and evidence refer to.**

Do not allow native Claude conveniences to create competing implementation lineage or silently change what is being verified.

---

# Human Interaction

A major requirement is that I can drop into individual Claudes whenever useful.

Distinguish:

1. answering a known Claude question / permission / elicitation / requested input

from

2. manually taking over an autonomous worker and changing its execution

These should not automatically have identical taint/replay semantics.

Borrow Claude's richer blocked-state concepts where practical:

- input needed
- permission prompt
- sandbox request
- worker request
- other blocked states

Manager should surface these clearly and let me respond with minimal friction.

I should always retain the escape hatch:

```text
Manager
→ inspect worker
→ enter real Claude session
→ steer it
→ return to Manager
```

---

# Agent Deck Concepts Worth Borrowing

Borrow concepts that support this product model, not Agent Deck's entire infrastructure.

## Persistent Conductor

Agent Deck's Conductor validates the Manager concept.

Keep the stronger Switchboard boundary:

```text
Manager reasons/proposes/routes.
Deterministic Switchboard state executes and remains authoritative.
```

## Explicit Relationships

Persist stable IDs and explicit relationships for:

- jobs
- workers
- parent/child
- workflows/runs
- roles

Never encode meaningful orchestration state in editable names or UI groups.

## Event-Driven Attention

Prefer reacting to semantic state transitions instead of unnecessary polling where possible.

Examples:

- working → waiting
- working → failed
- working → completed
- approval required
- evidence stale

Manager/attention should react to these transitions.

## Doorbell Rule

For future external integrations:

```text
external event
→ Manager/Switchboard
→ deterministic routing
→ worker
```

Never let GitHub/CI/file-watch/Telegram/etc. integrations directly spawn orphan workers outside the orchestration graph.

Do not necessarily build these integrations now.

## UX

Borrow useful terminal-native characteristics:

- dense but readable
- obvious status
- obvious attention
- minimal chrome
- fast keyboard interaction
- quick inspection/entry into workers

But Switchboard should organize primarily around **jobs / workflows / attention**, not merely a flat fleet of sessions.

Do not copy Agent Deck just for:

- generic multi-provider support
- MCP management
- skill management
- Docker infrastructure
- generic memory
- generic remote integrations
- generic session features Claude already provides

unless a concrete personal-use requirement exists.

---

# AWS CLI Agent Orchestrator Concepts Worth Borrowing

## Native Provider Passthrough

Prefer using Claude-native configuration instead of inventing Switchboard-specific equivalents.

Claude-specificity is an advantage.

Where appropriate, delegate:

- model
- effort
- permissions
- hooks
- skills
- sandbox
- native agent configuration

to Claude.

## Workflow Validation

Add or strongly consider:

```text
sb workflows validate
```

Workflow/config authoring mistakes should be detected before starting work.

Validate all invariants that can be checked statically or deterministically.

## Durable Orchestration

CAO validates the usefulness of a durable orchestration layer above native CLI agents.

Do not copy its arbitrary Python workflow execution runtime without a concrete need.

Prefer the constrained persisted Switchboard workflow representation because it is easier to reason about for:

- human approval
- bounded repetition
- recovery
- contracts
- evidence freshness
- deterministic state

---

# UX / Product Direction

Do not automatically reproduce the current Textual app one-for-one in another UI framework.

First determine what UI is actually necessary under this architecture.

The primary interface should emphasize:

- Manager conversation/input
- jobs
- workflow stage/progress
- workers associated with each job
- what needs my attention
- approvals
- blockers
- evidence/freshness
- readiness/completion
- quick inspection of an underlying Claude
- quick attach/enter/steer of an underlying Claude

Claude itself should render the actual coding-agent experience whenever practical.

A conceptual shape:

```text
Switchboard

ENG-123          implementing
startup race     needs approval
payments rebase  verifying

Manager:
> _
```

Selecting a job should make its actual workers easy to inspect and enter.

Whether this remains Textual, moves to `prompt_toolkit`/Rich, or becomes a simpler conversational terminal interface should follow from the desired interaction rather than preserving current screens.

Avoid unnecessary dashboard chrome.

---

# Personal-Use Constraint

This project is for one user.

Optimize for:

- simplicity
- maintainability
- speed
- low operational burden
- strong guarantees where they matter

Do not build infrastructure merely because a commercial multi-user product might need it.

Explicit non-goals unless a concrete need appears:

- generalized multi-provider support
- generic agent memory
- custom subagent/team framework
- custom general-purpose agent message bus
- custom review swarm
- custom OS sandbox
- Docker orchestration
- web control plane
- mobile client
- custom Slack/Telegram infrastructure when Claude already supports it
- generic MCP manager
- generic skill manager
- large plugin ecosystem
- arbitrary programmable workflow runtime

---

# Migration Discipline

Do not blindly rewrite the system.

For each subsystem classify it first:

## KEEP

- uniquely Switchboard semantic responsibility
- stronger proven invariant
- needed for durable workflow behavior

## DELEGATE

- Claude already provides it reliably and natively

## BORROW

- Agent Deck/CAO has a proven pattern that cleanly improves this architecture

## DELETE

- redundant machinery with a supported native replacement and no lost guarantee

## DEFER

- useful idea with no current personal-use need

Prefer simplification where safe.

Preserve proven invariants from prior phases:

- restart/recovery behavior
- exact workflow authority
- Git/worktree safety
- managed follow-ups
- evidence freshness
- deterministic completion
- independent verification
- human gates

Do not trade tested guarantees for architectural cleanliness without equivalent end-to-end verification.

---

# Required Deliverables

Carry this shift through to a coherent working implementation rather than stopping at a design document.

At minimum:

1. Audit the current repo against this model.
2. Produce a concise KEEP / DELEGATE / BORROW / DELETE / DEFER assessment.
3. Implement the highest-value changes required to make:

   ```text
   Manager → workflow → independent native Claude workers
   ```

   the obvious primary model.

4. Make Manager the natural primary entrypoint for receiving and managing work.
5. Make workflows clearly first-class peer recipes; `implement-contract` must not be hardcoded as the architecture itself.
6. Ensure independent workers remain individually visible, inspectable, attachable, and steerable.
7. Use native Claude capabilities more aggressively wherever supported and safe.
8. Incorporate useful Agent Deck / CAO concepts where they materially improve the personal workflow.
9. Simplify the UI around Manager + jobs/workflows + attention + worker inspection.
10. Add/update tests for changed invariants.
11. Perform real native end-to-end dogfood of the core interaction:
    - give Manager multiple pieces of work
    - Manager starts/manages multiple independent Claudes
    - workflows progress concurrently
    - blocked workers surface attention
    - inspect and enter an individual worker
    - steer/answer it
    - return to Manager
    - restart Switchboard and recover state
    - verify workflow/evidence semantics remain correct
12. Update architecture docs and README to reflect the resulting model.

---

# Success Criteria

The final architecture should make these statements true:

> **Switchboard replaces me as the human operator of many independent Claude Code sessions, while preserving my ability to inspect and steer any individual session.**

> **Workflows automate the procedures I would otherwise manually execute after opening each Claude.**

> **Claude Code owns the agents. Switchboard owns the workflow they are participating in.**

> **Switchboard is a conversational manager for running durable workflows across independent Claude Code sessions.**

Success is not measured by how much code changes.

The desired normal experience is:

```text
I receive work
→ I tell Manager what I want done
→ Manager chooses/runs the appropriate workflow
→ Manager launches and supervises ordinary independent Claude sessions
→ Claude uses native Claude capabilities internally
→ I only step into individual sessions when I want to inspect or steer
→ Switchboard keeps durable workflow state
→ Switchboard tells me when the work is actually complete
```
