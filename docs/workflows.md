# Workflows: goal, criteria, evidence

A workflow encodes a procedure you would otherwise carry out by hand after opening a
Claude. It is routing metadata plus a prompt: what it needs, what it produces, what it
invalidates, and whether it needs a fresh session. Workflows are YAML, and adding one
requires no change to Switchboard — including the role its workers play and the policy
they are launched with.

Every request has a goal, so every workflow shares one spine:

```
goal  ->  acceptance criteria  ->  evidence
```

What a workflow `produces` is that evidence, and it is also its **definition of done**:
the union of what its unconditional steps promise is exactly what a job following it must
have before Switchboard will call it complete. `sb workflows` prints it.

`complete-ticket` is one workflow among peers, not the architecture. `investigate`,
`diagnose-and-fix`, `rebase`, `review-only` and `answer-question` are its equals, each with
its own definition of done.

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
| `role_policy` | System-prompt policy for a role Switchboard has no built-in policy for. |
| `permission_mode` | Native Claude permission mode for its workers, overriding config. |
| `stage` | A free-text label starting it moves the job to. Descriptive; nothing gates on it. |
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

A conditional step is deliberately **not** part of the definition of done: a step that may
not run cannot be a precondition for finishing, or a change with no review findings would
be permanently unfinished. Its evidence is still checked when it exists.

## Checking a workflow before you rely on it

```bash
sb workflows validate
```

catches what is answerable from the definitions alone: a step naming a workflow that does
not exist, a composite that composes itself, a step needing evidence no earlier step
produces (and which workflows would supply it), a workflow no worker could run because its
own role is not in `allowed_roles`, one that requires what it produces, and a composite
whose unconditional steps produce nothing — so a job following it could never be reported
complete. It exits non-zero.

An unknown `{token}` in a prompt is not a problem: unmatched braces are left alone on
purpose, which is what lets a prompt carry a JSON schema or `git rev-parse HEAD^{tree}`
without escaping.

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

| Artifact | Question | Typically produced by |
| --- | --- | --- |
| `goal` | What is this trying to achieve, and what would establish it? | planner, investigator |
| `implementation_contract` | What shape should the solution take? | planner |
| `findings` | What is actually going on, and what is the evidence? | investigator, question |
| `verification` | What proof was observed, at which commit? | verifier |
| `review` | Does the change hold up to a fresh independent reading? | reviewer |
| `comment_resolutions` | What was done about each review comment? | implementer |

The goal carries each acceptance criterion's `evidence_required`; the verification report
carries the commands, exit codes, and observed behaviour that answer it. Nothing is
finished until the second matches the first at the current commit.

A `findings` report is the one that makes non-code work first-class. It carries an
`answer` that stands on its own plus evidenced findings, so asking a Claude something
leaves a durable result rather than a transcript — and a later fix worker can be handed
that artifact verbatim instead of somebody's paraphrase of it.

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

## Completion

`job_completion` is computed in `core/evidence.py` from stored state, never asserted by a
model, and what it asks for comes from the job's workflow rather than from a fixed list.
For each artifact that workflow's unconditional steps promise, it requires the artifact to
exist, to still apply to current HEAD, and to satisfy the check its type carries:

- an implementation contract must be approved and free of unanswered blocking decisions;
- a goal must have criteria, each passed or carrying an accepted limitation;
- a verification report must pass;
- a review must have no unresolved blocking findings;
- a findings report must state an answer, and every non-speculative finding needs evidence;
- comment resolutions must not still need a human decision.

It also requires a clean authoritative worktree when the workflow mutates code, a run that
has finished, and every child job complete. A job following no workflow is reported
unfinished: an empty checklist is not a satisfied one.

`check_completion` is the Manager's tool for this, and it is told to report what the gate
says rather than judge. `verification_blurb` builds a copy-pastable summary from the same
stored evidence.

## Splitting a request across jobs

Some work is genuinely separable — diagnosing before anyone can fix, changes in two
repositories, an investigation whose answer decides what to do next. Manager can create a
job per part:

- `context_job_ids` names the jobs whose stored artifacts this job's workers are given.
  The artifact travels from the store verbatim, and satisfies a prerequisite: input may be
  borrowed.
- `parent_job_id` says the parts serve one request. The parent is not complete until its
  children are.

Completion deliberately reads only a job's *own* evidence. Output may not be borrowed.

Do not split what a single workflow already expresses: `diagnose-and-fix` already runs
diagnosis, fix, verification and review as four independent sessions inside one job.

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
