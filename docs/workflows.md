# Workflows, contracts, and evidence

A workflow is routing metadata plus a prompt: what it needs, what it produces, what it
invalidates, and whether it needs a fresh Claude. Workflows are YAML, and adding one
requires no change to Switchboard.

## Atomic and composite

An **atomic** workflow carries a `prompt` and runs on one independent Claude worker. A
**composite** workflow carries `steps` that name other workflows. Both are the same type
loaded from the same YAML, which is how the development ritual itself becomes
configurable — `complete-ticket` is a composite of the same atomic workflows you can
invoke directly.

There is deliberately no graph, no branching, and no expression language: a sequence, five
named conditions, and a bounded repeat count.

```yaml
name: post-rebase-verify
description: Re-verify a change after rebasing it, because the old evidence no longer holds.
steps:
  - workflow: rebase-stack
  - workflow: full-verify
  - workflow: smoke-test
```

### Fields

| Field | Meaning |
| --- | --- |
| `name` | Canonical name. Defaults to the filename. |
| `description` | What the manager routes on. |
| `aliases` | Other names that resolve to this one. |
| `role` | The worker role this runs as. |
| `allowed_roles` | Roles it may be targeted at. Defaults to `[role]`. |
| `mutates_code` | Whether it needs a writable worker and an isolated worktree. |
| `requires` | Artifacts that must exist *and be current* before it may run. |
| `produces` | The artifact harvested from its fenced JSON block. |
| `invalidates` | Extra artifact types a run of this workflow makes stale. |
| `context` | Extra stored artifacts to put in the prompt beyond `requires`. |
| `stage` | The job stage starting it moves the job to. |
| `worker` | `fresh`, `existing`, or `auto`. |
| `prompt` | The template, for an atomic workflow. |
| `steps` | The step list, for a composite. |

### Step fields

| Field | Meaning |
| --- | --- |
| `workflow` | The workflow this step runs. |
| `when` | `always`, `human-decisions`, `code-changed`, `verification-failed`, `blocking-findings`. |
| `approval` | `none`, `required`, `only-if-decisions`. |
| `worker` | `fresh`, `existing`, `auto`. |
| `max_iterations` | Bounded repeat, 1–10. |

Every `when` condition is answered from stored state and Git lineage — never from a model
remembering that something changed. That is what makes a run resumed tomorrow evaluate its
next step exactly as it would have today.

## Where workflows come from

Built-ins ship inside the package, then `~/.switchboard/workflows`, then each registered
repository's `.switchboard/workflows`. Later sources add to earlier ones and override each
other by name — which is how a repository states a convention its contributors should get
over a user's own version of the same idea.

**Built-in names are reserved.** A workflow's `requires` and `mutates_code` are what
enforce contract prerequisites and worktree isolation, both default to permissive, and a
repository's workflows travel inside the repository they would be constraining. A file
merely reusing a built-in name — stating nothing — would silently strip both, so the
loader refuses it and reports the problem. A malformed file is reported and skipped, never
raised: one broken user workflow must not stop Switchboard from starting.

`sb workflows` lists everything this installation loaded.

## Contracts and evidence

What makes delegation reliable is not a better prompt; it is an executable contract around
the agent.

| Artifact | Question | Produced by |
| --- | --- | --- |
| Implementation contract | What shape should the solution take? | planner |
| Behavior contract | What must observably work, and what proof would show it? | planner |
| Verification report | What proof was actually observed, at which commit? | verifier |

The behavior contract carries each acceptance criterion's `evidence_required`; the
verification report carries the commands, exit codes, and observed behaviour that answer
it. Nothing is finished until the second matches the first at the current commit.

They are stored as structured artifacts, not prose in a transcript. A worker emits a
fenced ```json block; `extract_json_block` plus Pydantic validation turn it into an
artifact. The model never writes the database.

Dispatch is on what the workflow declares it *produces*, not on its name, so a
user-defined workflow that produces a verification is harvested exactly like a built-in
one.

## Prerequisites

`_assert_prerequisites` refuses a workflow whose declared `requires` are missing or stale.
For a code-mutating workflow requiring an implementation contract it goes further: the
contract must be **approved** and carry no unanswered blocking decisions.

This is what stops implementation from starting without an approved plan, however
confidently a model asks for it.

## Freshness and invalidation

Freshness is decided from Git head and tree hashes alone.

- Same head and tree → nothing changed.
- Same tree, different head → a restack or reword: behavioural evidence still holds, so
  only lineage moves forward.
- Different tree → an implementation edit: verification, smoke verification, and review go
  stale, plus anything the running workflow declares it `invalidates`.

The baseline is captured before any writable worker's turn — from writability, not from
intent, because any turn can change the tree — and stored on the runtime, so a controller
that dies mid-turn still learns on restart that the code moved.

Only the job's **authoritative worktree** is inspected. Other writable workers stay
isolated and cannot silently become the change under review.

## Composite runs

A `WorkflowRun` is persisted, so a run survives a restart, and `core/runs.py` evaluates
every condition from stored state alone. The only backwards move is a bounded repeat, so a
run provably terminates. Safety invariants are never configurable from a workflow file.

Advancement authority is explicit and durable:

- reserving a worker and sending it a prompt do **not** complete a step;
- only a successfully applied, manager-owned terminal event marks it complete;
- artifact harvesting and that completion marker share the hook-application transaction;
- recovery adopts the exact live runtime and advances a marked step exactly once;
- loss of an incomplete runtime blocks rather than resending or advancing;
- a failed turn never sets completion authority;
- human ownership taints the attempt, pauses the run, and an explicit resume replays the
  same bounded step from durable contracts without consuming an iteration.

A step that stops to ask the user pauses its run; answering the worker puts it back in
flight. A step with an approval gate raises a `plan_approval` attention item pointing at
the worker whose session explains the gate — or at nothing, rather than at an unrelated
worker the user happens to have started.

## Ready to push

`ready_to_push` is computed in `core/evidence.py` from stored state, never asserted by a
model. It reports every blocker it finds:

- no authoritative change worktree;
- no implementation contract, or an unapproved one, or unanswered blocking decisions;
- no acceptance criteria, or a criterion that is not passed and has no accepted limitation;
- no verification evidence, or evidence that does not apply to current HEAD;
- no independent review, a stale one, or unresolved blocking findings;
- uncommitted changes in the worktree.

`verification_blurb` builds a copy-pastable summary from the same stored evidence.

## Mining

`mine-workflows` is an ordinary read-only workflow whose input is
`SessionManager.workflow_history()` — Switchboard's own record of what ran, in order, per
job. It produces `WORKFLOW_PROPOSALS`, which are inert. Only `accept_proposal` writes one
out, as an ordinary user workflow file with no marker distinguishing it: an accepted
proposal *is* a workflow, not a second-class kind of one.

Everything a model chose is validated before anything is written — unknown step workflows,
nested composites, reserved built-in names, and the resulting document itself — because
writing first and failing at load time would tell the user their workflow exists when it
does not.
