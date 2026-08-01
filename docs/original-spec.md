# Goal: Build the Claude Session Manager

> **Historical document.** This is the specification as originally written, kept
> unedited. The project has since been named **Switchboard**: the package is
> `switchboard`, the command is `sb`, variables are `SB_*`, and state lives under
> `~/.config/switchboard/` and `~/.local/share/switchboard/`. Read the names below as
> historical; see [`../README.md`](../README.md) and [`../CLAUDE.md`](../CLAUDE.md) for
> what the code actually does now.

> **This file is the complete product and implementation specification.**
> Implement the application described here end to end. Do not depend on a separate README, handoff document, or prior conversation.

## 0. Instructions to the implementation agent

Build a working personal prototype, not a design mockup.

- Work autonomously until the acceptance criteria are met or a genuine external blocker is reached.
- Prefer a narrow, complete implementation over a broad collection of stubs.
- Do not broaden this into a general multi-agent platform.
- Do not ask the user to choose routine implementation details; select reasonable defaults and document them.
- Keep Git, worktree, cleanup, routing, and state-transition safety deterministic. Do not rely on model memory or prompt compliance for invariants.
- Run the application, tests, and a realistic local smoke test before declaring completion.
- Produce the evidence deliverable described at the end.

### 0.1 Meta requirements for implementing this goal

The agent implementing this application must dogfood the core development practices the product is intended to support:

1. **Protect the primary implementation context.** Use bounded subagents when reconnaissance, research, testing, review, or an isolated implementation slice would otherwise pollute the main context. Give each subagent a narrow objective, only the relevant context, explicit file ownership when it may edit, and a concrete expected output. Do not use unbounded fan-out or overlapping writable ownership.
2. **Produce a clean atomic commit stack.** Plan the intended commits before implementation, keep each commit coherent and reviewable, separate unrelated refactors from behavior changes where practical, include tests with the behavior they validate, and avoid WIP/checkpoint or artificial micro-commits in the final stack.
3. **Apply the same contract-driven loop to this build.** Before substantial implementation, establish a concise implementation contract, behavior/acceptance contract, and evidence contract for the prototype. Implement against them, verify every criterion, then use a fresh independent reviewer and address valid findings before completion.

These requirements govern how this goal itself is implemented. They do not require the implementation agent's user-facing narration to use the product's runtime concision policy; that policy belongs to the finished application.

### 0.2 Runtime scope distinction

The communication policy in Section 4.4 applies to the **finished product's manager and worker sessions**. The runtime subagent and commit-discipline requirements in Sections 8 and 11 also describe behavior the finished product must support, in addition to the implementation-process requirements above.

---

## 1. Product summary

Build a one-window control plane for multiple **independent Claude coding sessions**.

The user wants the benefits of running several ordinary Claude instances in parallel without repeatedly:

1. opening terminals;
2. finding or creating the correct worktree;
3. starting Claude in the correct directory;
4. remembering which terminal belongs to which task;
5. checking every terminal to see which session is blocked or complete;
6. preventing sessions from clobbering one another;
7. cleaning up processes, branches, and worktrees safely.

The application should feel like an inbox and IDE for parallel Claude sessions:

```text
┌──────────────────────────────┬──────────────────────────────────────────────┐
│ Manager Claude               │ Selected worker                              │
│                              │                                              │
│ > Take ticket ENG-421        │ Full independent Claude conversation         │
│ > Rebase this stack          │                                              │
│ > What needs me?             │                                              │
├──────────────────────────────┤                                              │
│ Workers / attention queue    │                                              │
│ ! Auth fix · needs input     │                                              │
│ ● ENG-421 · implementing     │                                              │
│ ✓ Cache bug · ready to push  │                                              │
└──────────────────────────────┴──────────────────────────────────────────────┘
```

### Core promise

The user expresses intent once. The manager determines whether to:

- route the request to the appropriate existing worker;
- invoke the appropriate reusable workflow on that worker;
- create a new independent worker in the correct repository/worktree;
- create a fresh independent reviewer or verifier;
- ask the user only when human judgment is genuinely required.

The user can open and interact with every worker directly as though it were its own normal Claude session.

---

## 2. Product boundaries

### 2.1 Manager/control plane

The manager is aware of:

- registered repositories;
- jobs/tickets;
- workers and their roles;
- which workers are related to the same job;
- branches and worktrees;
- current commit hashes;
- worker status and attention reasons;
- reusable workflows;
- implementation, behavior, and evidence contracts;
- review and verification freshness.

The manager is a natural-language router, command palette, and status summarizer. It is **not** the source of truth.

### 2.2 Independent workers

Every worker must have:

- an independent Claude session and context;
- an independent transcript;
- an assigned repository working directory;
- its own worktree when it may edit code;
- direct user interaction in the right pane;
- no access to the global worker registry;
- no awareness of other workers unless the manager explicitly passes a bounded artifact such as an approved plan or review report.

Workers must not communicate with one another directly. Coordination occurs through structured artifacts and the manager.

### 2.3 What this is not

Do not build:

- a chat room for agents;
- an autonomous organization with unlimited fan-out;
- a replacement for Git or Claude's coding tools;
- automatic merging, force-pushing, or destructive cleanup without approval;
- a multi-user or remote orchestration service;
- an elaborate plugin marketplace;
- a generic workflow engine unrelated to coding sessions.

The purpose is to remove mechanical coordination while preserving human judgment.

---

## 3. Feasibility and technical basis

Use the official Python Claude Agent SDK as the programmatic agent runtime.

The SDK provides persistent interactive sessions, streaming, interrupts, hooks, custom tools, session resume, project instructions, and skill support. Maintain one long-lived worker session per active worker. Sessions persist conversation state; Git worktrees persist filesystem state.

The prototype should be entirely Python application code. It may invoke external `git` and the Claude runtime supplied by the SDK.

### Locked stack

- Python 3.12+
- Textual for the terminal UI
- `asyncio` for concurrency and event routing
- `claude-agent-sdk` for manager and worker agents
- Pydantic v2 for validated domain models and structured artifacts
- SQLite via the standard library for durable state
- Git via argument-array subprocess calls
- Pytest, pytest-asyncio, and temporary Git repositories for tests
- `uv` or a standard `pyproject.toml` workflow for setup

Do not introduce Electron, React, TypeScript, or Node as application dependencies.

### Important design choice

Use the Agent SDK backend first. Define a `WorkerBackend` protocol so a native `claude` CLI/PTY backend can be added later, but do not make terminal emulation a prerequisite for the prototype.

The Agent SDK workers still satisfy the important semantics: independent contexts, independent working directories, direct interaction, and normal coding-agent capabilities.

---

## 4. User experience

### 4.1 Three-pane layout

**Top-left: Manager**

- One universal natural-language input for every intent.
- Pasting a ticket is just a normal manager message; do not add a separate ticket form, intake panel, wizard, or required mode switch.
- The manager infers whether the message is a new ticket, follow-up, question, rebase, review-comment batch, verification request, or cleanup request.
- Brief, plain-English manager responses and routing confirmations.
- Default to the smallest useful response: outcome, blocker, or next action.
- Bounded recent conversation, not an unbounded transcript.

**Bottom-left: Worker list and attention inbox**

Each row shows:

- title;
- job/ticket identifier when present;
- repository;
- worker role;
- stage;
- status;
- concise attention reason;
- optional model label.

**Right: Selected worker**

- Complete worker transcript.
- Streaming assistant output and tool activity.
- Normal follow-up input.
- Interrupt control.
- An application-owned banner explaining why the worker needs attention.

Example:

```text
ENG-421 · Planner · Needs input
Reason: Two backwards-compatible persistence strategies are viable.
Waiting for: Choose whether legacy records must remain writable.
```

### 4.2 Keyboard-first controls

Implement discoverable bindings, with these defaults unless Textual conflicts:

- `Ctrl+N`: focus manager input
- `Ctrl+J` / `Ctrl+K`: next/previous worker
- `Ctrl+Enter`: send current message
- `Ctrl+Space`: next attention item
- `Ctrl+P`: pin/unpin selected worker
- `Ctrl+S`: snooze selected attention item
- `Ctrl+A`: toggle auto-advance
- `Ctrl+C` or explicit button: interrupt selected worker safely
- `Esc`: return focus to worker list/manager
- `?`: show help

Do not switch workers while the user is typing.

### 4.3 Attention queue and auto-advance

Prioritize actionable items in this order:

1. human decision required;
2. permission/sandbox input required;
3. worker failure;
4. plan approval required;
5. blocking review finding;
6. verification failure;
7. ready for review;
8. ready to push;
9. completed cleanup candidate.

Auto-advance behavior:

1. The user responds to the current blocked worker.
2. The worker returns to `working` or the item is marked handled.
3. If auto-advance is enabled and the current worker is not pinned, open the next actionable worker.
4. If nothing requires attention, return focus to the manager/dashboard.

Support pin, snooze, and pause-auto-advance.


### 4.4 Runtime communication policy

The finished product must make every manager and worker **maximally concise by default**, while preserving reasoning quality, tool use, and implementation capability.

This is a presentation policy, not a request for shallower reasoning. Agents may reason, inspect, test, and use tools as deeply as necessary, but should expose only the information the user needs to decide or act.

Apply a stable runtime instruction to the manager and every worker role:

```text
Use plain English and be maximally concise. Think and investigate as deeply as
needed, but show only the conclusion, action taken, blocker, evidence summary,
or next decision. Do not repeat known context, narrate routine tool use, or give
long background explanations. Ask one concrete question at a time, with short
options and a recommendation when useful. Put detailed logs, commands, traces,
and supporting evidence in artifacts or collapsed detail views. Expand only
when the user asks.
```

Role-specific defaults:

- **Manager:** usually one to three sentences: route taken, current status, blocker, or next action.
- **Planner:** main plan is at most 10 short lines, followed only by material decisions, acceptance criteria, and commit stack.
- **Implementer:** progress updates are normally one sentence; completion reports only changed behavior, commits, verification status, limitations, and next action.
- **Verifier:** verdict first, then criterion-level failures or limitations; passing details live in evidence artifacts.
- **Reviewer:** verdict first; show only actionable findings by default, ordered by severity. Keep optional nits collapsed.
- **Question worker:** answer directly before explanation; avoid broad tutorials unless requested.

The UI should support explicit expansion without changing the default:

- `expand` or `more detail` asks the current agent to elaborate;
- detailed tool output and evidence remain inspectable;
- a per-session verbosity override may offer `concise` (default), `normal`, and `detailed`;
- changing verbosity affects displayed explanation, not reasoning effort or available tools.

Never omit a blocker, safety concern, failed criterion, uncertainty, or material limitation merely to stay short.

---

## 5. Canonical application state

The manager transcript must never be the system of record.

Persist the following in SQLite:

- repositories;
- jobs;
- workers;
- worker sessions;
- branches/worktrees and ownership;
- events;
- attention items;
- user decisions;
- workflow executions;
- implementation contracts;
- behavior contracts / acceptance criteria;
- evidence contracts and verification results;
- review findings;
- artifact-to-commit lineage;
- user preferences and model policy.

### 5.1 Suggested domain models

Use explicit Pydantic models and enums. The exact schema may evolve, but preserve these concepts.

```python
class WorkerStatus(str, Enum):
    STARTING = "starting"
    WORKING = "working"
    BLOCKED = "blocked"
    IDLE = "idle"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"
    DISCONNECTED = "disconnected"

class JobStage(str, Enum):
    INTAKE = "intake"
    PLANNING = "planning"
    AWAITING_INPUT = "awaiting_input"
    IMPLEMENTING = "implementing"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    FIXING = "fixing"
    READY_TO_PUSH = "ready_to_push"
    COMPLETED = "completed"
    FAILED = "failed"

class WorkerRole(str, Enum):
    GENERAL = "general"
    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    VERIFIER = "verifier"
    REVIEWER = "reviewer"
    QUESTION = "question"
    REBASE = "rebase"
    REVIEW_COMMENTS = "review_comments"
```

```python
class Repository:
    id: UUID
    name: str
    root_path: Path
    default_branch: str
    registered_at: datetime

class Job:
    id: UUID
    title: str
    external_ref: str | None
    repository_id: UUID
    stage: JobStage
    selected_worker_id: UUID | None
    base_ref: str
    created_at: datetime
    updated_at: datetime

class Worker:
    id: UUID
    job_id: UUID | None
    title: str
    role: WorkerRole
    status: WorkerStatus
    repository_id: UUID
    cwd: Path
    worktree_id: UUID | None
    session_id: str | None
    model: str | None
    waiting_for: str | None
    writable: bool
    pinned: bool
    snoozed_until: datetime | None
    created_at: datetime
    updated_at: datetime
```

### 5.2 Events

Treat lifecycle changes as events:

```text
worker.created
worker.started
worker.output
worker.blocked
worker.permission_required
worker.resumed
worker.failed
worker.completed
worker.stopped
plan.created
plan.requires_input
plan.approved
verification.started
verification.passed
verification.failed
review.started
review.blocking_findings
review.passed
job.ready_to_push
artifact.invalidated
cleanup.refused
cleanup.completed
```

Events drive persistence, status changes, notifications, and the attention queue.

---

## 6. Manager design and automatic context management

### 6.1 Keep the manager bounded and replaceable

Do not maintain one indefinitely growing manager context.

Use a mostly stateless manager turn:

1. Store durable information in SQLite.
2. For each manager request, construct a compact state snapshot.
3. Include the current user request, selected job/worker, active attention items, relevant recent events, available workflows, and a small recent chat window.
4. Invoke the manager model with constrained custom tools.
5. Persist any resulting actions and user-visible response.

Suggested bounds:

- at most 8 recent manager exchanges;
- at most 8 active workers in detail;
- summarize additional workers by status count;
- at most 10 recent relevant events;
- include completed jobs only when explicitly referenced;
- never include full worker transcripts by default.

The manager can be restarted without losing operational state.

### 6.1.1 Manager response policy

Include a stable system instruction in every manager invocation:

```text
Be maximally concise and use plain English. Preserve reasoning quality, but show
only the conclusion, routing action, blocker, or next user decision. Do not
repeat known context. Prefer one short paragraph or a compact list. Ask one
concrete question at a time. Offer more detail only when requested.
```

Manager responses should normally fit one of these shapes:

```text
Started ENG-421 in a new isolated worktree. Planning is in progress.
```

```text
ENG-421 needs one decision: preserve writes to legacy records?
A. Yes
B. Read legacy, write new format only (recommended)
C. Drop legacy support
```

```text
Three workers need attention: one decision, one failed test, one change ready to push.
```

Do not expose internal routing deliberation or verbose worker summaries by default.

### 6.2 Manager tools

Expose constrained in-process tools, not arbitrary shell access:

```text
register_repository(path, name?)
list_repositories()
list_jobs(status?)
list_workers(job_id?, status?)
inspect_worker(worker_id)
create_job(title, repository_id, external_ref?, base_ref?)
create_worker(job_id?, role, title, prompt, writable, model?, workflow?)
route_message(worker_id, message, workflow?)
open_worker(worker_id)
interrupt_worker(worker_id)
stop_worker(worker_id)
request_cleanup(worker_id | job_id)
list_attention_items()
record_decision(job_id, question, answer)
start_workflow(job_id, workflow_name, target_worker_id?)
```

The tool handlers must validate permissions, repository identity, worktree ownership, and state transitions.

### 6.3 Routing policy

The manager proposes a route; deterministic code validates it. The same manager input handles tickets and all other requests. There is no separate ticket-ingestion UI or command.

For a pasted ticket, the manager should internally:

1. extract a concise title and external identifier when present;
2. identify an existing related job/worker or create a new job;
3. resolve the repository from explicit text, selected context, registered-repository metadata, and repository hints in the ticket;
4. select the smallest appropriate workflow bundle and risk tier;
5. create the required independent worker sessions and isolated worktree;
6. seed them with the ticket and only the relevant structured artifacts;
7. ask one concise question only when unresolved ambiguity could route or modify the wrong repository/change.

For a normal feature ticket, the default workflow bundle is the implementation, behavior, and evidence contract loop followed by implementation, verification, and fresh independent review. Trivial or non-code tickets may use a smaller workflow.

Routing priority:

1. explicit worker/job/ticket/branch/PR reference;
2. currently selected worker/job;
3. matching existing primary worker for the job;
4. semantic operation type;
5. create a new worker;
6. ask one concise routing question only if ambiguity could cause a harmful action.

Examples:

```text
“Rebase this”
→ selected job's existing writable worker
→ invoke `rebase-stack`

“Address these review comments”
→ job's implementation worker
→ invoke `address-review-comments`
→ invalidate stale review/verification

“Run another smoke test”
→ fresh or existing verifier against current HEAD
→ invoke `smoke-test`

“Rereview it”
→ fresh independent reviewer against current HEAD
→ invoke `review-change`

“<paste full ENG-421 ticket text>”
→ extract ticket identity/title
→ route to an existing related job if one exists, otherwise create a job
→ resolve the correct repository
→ select the appropriate workflow bundle
→ create planner/implementation workers as required in isolated worktrees
→ begin the contract-driven feature workflow

“Why is this cache shared?”
→ route to selected job's worker if context matters
→ otherwise create a temporary read-only question worker
```

A destructive route must require explicit confirmation even when the manager is confident.

---

## 7. Worktree and Git safety

The application—not Claude—owns worktree allocation and cleanup.

### 7.1 Invariants

- At most one writable worker owns a worktree.
- Writable workers for the same repository use separate worktrees.
- Reviewers and question workers are read-only by default.
- Never delete a worktree with uncommitted changes.
- Never delete commits that are not reachable from a protected ref or explicitly acknowledged as disposable.
- Never force-push, merge, delete a branch, or discard changes without explicit user approval.
- Use subprocess argument arrays; do not construct shell command strings from user/model input.
- Validate all paths are within registered repository/worktree roots.
- Cleanup is conservative and idempotent.

### 7.2 Worktree service

Implement a service with operations such as:

```text
create_worktree(repository, job, worker, base_ref)
inspect_worktree(worktree_id)
get_head(worktree_id)
get_dirty_state(worktree_id)
get_unpushed_commits(worktree_id)
can_cleanup(worktree_id)
cleanup_worktree(worktree_id)
```

Use predictable paths under an application data directory or a configurable worktree root, for example:

```text
~/.local/share/claude-session-manager/worktrees/<repo>/<job>-<worker>/
```

Do not place application metadata inside the user's source repository unless explicitly configured.

---

## 8. Worker backend

Define an interface so orchestration does not depend directly on SDK details:

```python
class WorkerBackend(Protocol):
    async def start(self, spec: WorkerSpec) -> WorkerHandle: ...
    async def send(self, worker_id: UUID, message: str) -> None: ...
    async def stream(self, worker_id: UUID) -> AsyncIterator[WorkerEvent]: ...
    async def interrupt(self, worker_id: UUID) -> None: ...
    async def stop(self, worker_id: UUID) -> None: ...
    async def resume(self, worker: Worker) -> WorkerHandle: ...
    async def health(self, worker_id: UUID) -> BackendHealth: ...
```

### Agent SDK backend requirements

- Use a separate `ClaudeSDKClient` or independently resumed session for every active worker.
- Set each worker's `cwd` to its assigned repository/worktree.
- Capture and persist session IDs.
- Stream text, tool use, results, questions, permissions, failures, and completion into normalized application events.
- Support follow-up messages and interrupts.
- On app restart, resume sessions when possible; otherwise mark them `disconnected` and offer a safe replacement seeded from structured artifacts.
- Load project/user Claude settings deliberately. Document whether `setting_sources` includes `user` and `project`.
- Do not expose manager tools or global registry state to workers.

### Runtime prompt composition

Every manager and worker invocation must compose its system instructions from:

1. the normal Claude/Claude Code coding-agent instructions supplied by the SDK;
2. the global concise plain-English policy in Section 4.4;
3. the worker's role-specific workflow policy;
4. repository instructions and user preferences;
5. only the structured job artifacts relevant to the current action.

Do not replace the normal coding-agent capabilities with a minimal custom chatbot prompt. Append the product policy so concision changes presentation rather than reasoning or tool quality.

Persist the effective prompt-policy version with each session so behavior is reproducible and migrations can restart or refresh older sessions safely.

### Internal subagent policy

Implementation workers may use internal subagents as bounded context-isolation helpers. These helpers are **not** top-level workers in the manager's global session list and do not violate the requirement that top-level workers remain independent.

The primary implementation worker remains the sole owner of the job, worktree, accepted plan, commit stack, integration, and final answer.

Runtime rules:

- Use subagents only when a task has a clear bounded slice or when independent exploration would otherwise pollute the primary worker's context.
- Good uses include repository reconnaissance, locating call sites, isolated API research, building a focused test, checking one subsystem, or performing a fresh local review.
- Give each subagent only its objective, relevant files/interfaces, constraints, and expected output. Do not forward the full parent transcript.
- Prefer read-only subagents that return findings, test plans, or patches for the primary worker to integrate.
- If a subagent may edit, assign explicit non-overlapping file ownership and prevent concurrent writes to the same files.
- Do not allow nested or unbounded fan-out. Make concurrency and total-helper limits configurable; default to at most 3 active helpers for one implementation worker.
- Subagents report concise results. The primary worker verifies their work and remains accountable for all claims.
- Subagent completion is not evidence. The primary worker must inspect changes and run the relevant checks.
- For trivial or tightly coupled work, do not spawn helpers merely to satisfy a ritual.

Show only a compact activity indicator in the main UI, such as `2 helpers active`. Detailed helper activity may be inspectable, but should not flood the worker conversation.

### Worker transcript

Persist either normalized transcript messages or sufficient references to reload them. The right pane must show prior messages after selection and app restart.

---

## 9. Reusable workflow blocks

Implement workflows as first-class, composable application capabilities. They may be backed by prompt templates initially and can later map to filesystem Claude Skills without changing the domain API.

Required workflow names:

```text
plan-feature
implement-approved-plan
smoke-test
full-verify
review-change
address-review-comments
rebase-stack
restack-commits
rereview
answer-codebase-question
finalize-change
```

Each workflow declares:

```python
class WorkflowDefinition:
    name: str
    description: str
    allowed_roles: set[WorkerRole]
    required_artifacts: set[ArtifactType]
    produced_artifacts: set[ArtifactType]
    mutates_code: bool
    invalidates: set[ArtifactType]
    default_model_role: str
```

The manager selects the workflow; the target worker executes it. User and repository preferences are loaded from configuration, for example:

```yaml
# ~/.config/claude-session-manager/config.yaml
communication:
  style: concise_plain_english
  default_verbosity: concise
  status_max_sentences: 2
  default_expand_details: false
  plan_max_lines: 10
  collapse_tool_output: true

subagents:
  enabled: true
  max_concurrent_per_worker: 3
  prefer_read_only: true
  allow_nested: false

commits:
  require_plan: true
  atomic_by_default: true
  allow_wip_commits: false
  test_before_commit: true

models:
  planner: "<strong-model>"
  implementer: "<cost-effective-model>"
  reviewer: "<strong-model>"
  verifier: "<cost-effective-model>"

workflows:
  rebase-stack:
    preserve_merges: false
    autosquash_fixups: true
    never_force_push: true
  plan-feature:
    max_plan_lines: 10
  review-change:
    blocking_severities: [blocking, important]
```

Do not hardcode current model product names. Treat model IDs as user configuration with sensible environment-based defaults.

---

## 10. Three contracts for trusted implementation

The standard feature loop is based on three explicit contracts.

### 10.1 Implementation contract

Answers: **What shape should the solution take?**

The planner must produce a concise plan with:

- no more than 10 short lines in the main plan;
- the main components/files and implementation shape;
- material architectural or compatibility considerations;
- every point where human input changes the result;
- a proposed atomic commit stack;
- risks or assumptions that could invalidate the approach.

Long hidden analysis is fine; the user-facing plan must be concise.

Use structured output:

```python
class ImplementationContract(BaseModel):
    summary_lines: list[str]  # max 10
    decisions: list[DecisionRequest]
    commit_stack: list[CommitSpec]
    risks: list[str]
    base_commit: str
```

Every human decision should present concrete options, a recommendation, and whether it blocks implementation.

### 10.2 Behavior contract

Answers: **What must observably work?**

Acceptance criteria describe externally observable behavior rather than implementation activity.

Each criterion includes:

```python
class AcceptanceCriterion(BaseModel):
    id: str
    behavior: str
    verification_method: str
    evidence_required: list[str]
    status: Literal["pending", "passed", "failed", "blocked"]
```

A standard feature must include the deepest practical end-to-end or smoke test permitted by the environment. This should trace the real data/control flow rather than only run isolated unit tests.

### 10.3 Evidence contract

Answers: **What proof demonstrates each behavior?**

Evidence can include:

- exact commands and exit codes;
- unit/integration/E2E results;
- relevant logs;
- persisted state/database reads;
- request/response traces;
- screenshots for UI behavior;
- explicit limitations when credentials, network, or environment access prevent a true end-to-end test.

“Tests passed” without criterion-specific evidence is insufficient.

```python
class VerificationEvidence(BaseModel):
    criterion_id: str
    status: Literal["passed", "failed", "not_tested", "blocked"]
    commands: list[CommandEvidence]
    observed_behavior: str
    artifacts: list[str]
    limitations: list[str]
    tested_head: str
    created_at: datetime
```

---

## 11. Standard feature workflow

Implement this state machine:

```text
INTAKE
  ↓
PLAN
  ↓
HUMAN DECISIONS / PLAN APPROVAL, when required
  ↓
IMPLEMENT
  ↓
VERIFY AGAINST ACCEPTANCE CRITERIA
  ↓
FRESH INDEPENDENT REVIEW
  ↓
FIX → REVERIFY → REREVIEW, as needed
  ↓
READY TO PUSH
```

### 11.1 Plan

- Start a planner worker, normally read-only.
- Inspect the ticket and repository.
- Produce implementation, behavior, and evidence contracts.
- Ask interactive questions only for material decisions.
- Require explicit approval when the change is substantial or risky.

### 11.2 Implement

- Start or reuse an implementation worker in a writable worktree.
- Seed it only with the ticket, approved contracts, decisions, repository instructions, and commit plan—not the planner's private reasoning.
- Implement in the proposed atomic commit stack where practical.
- Let the implementation worker spawn bounded subagents when this keeps its context clean or safely parallelizes independent work.
- Give every subagent explicit scope, file ownership, expected output, and checks; never allow overlapping writable ownership.
- Keep subagent outputs concise and integrate them through the primary implementation worker.
- Continue until criteria are met or a genuine blocker is found.
- Model policy may use a stronger planner/reviewer and a cheaper implementer, but routing is configurable rather than hardcoded.

#### 11.2.1 Runtime implementation and commit discipline

The implementation worker must treat the approved implementation contract as an execution plan, not a loose suggestion.

- Work through the planned commit stack in order unless repository discoveries require a change.
- If the commit shape changes materially, update the implementation contract and surface the change concisely before proceeding when human input matters.
- Create coherent, reviewable commits with one primary purpose each.
- Keep refactors separate from behavior changes when practical.
- Include tests with the behavior they validate.
- Avoid `WIP`, checkpoint, or mixed-purpose commits in the final stack.
- Avoid artificial micro-commits that make the stack harder to review.
- Run the relevant focused checks before each commit when practical.
- Keep the branch in a usable state at commit boundaries where practical.
- After implementation, compare the actual stack with the approved stack and explain any differences.

The primary implementation worker owns all commits. Internal subagents may return findings or patches, but should not independently create competing commit histories unless the workflow explicitly assigns a non-overlapping commit slice and the primary worker verifies and integrates it.

Expose compact commit progress in the UI, for example:

```text
ENG-421 · Implementing · commit 2/3: persist notification preferences
```

### 11.3 Verify

- Invoke `full-verify` for substantial changes; `smoke-test` for targeted reruns.
- Verify every acceptance criterion.
- Record evidence against the exact current HEAD.
- Treat untested criteria and environment limitations honestly.

### 11.4 Independent review

Start a fresh independent reviewer with:

- original request/ticket;
- approved contracts and decisions;
- base and head commit;
- complete diff and commit stack;
- verification evidence.

Do not provide the implementer's private reasoning or self-assessment.

The reviewer evaluates:

1. implementation correctness;
2. whether the acceptance criteria were met;
3. whether the original plan/criteria missed important behavior;
4. architecture, security, maintainability, and commit quality.

```python
class ReviewFinding(BaseModel):
    id: str
    severity: Literal["blocking", "important", "minor", "nit"]
    category: str
    description: str
    evidence: str
    recommended_action: str
    reviewed_head: str
```

### 11.5 Fix loop

Blocking or important valid findings return the job to fixing. Any code mutation invalidates stale verification and review as defined below.

### 11.6 Ready to push

A job becomes `ready_to_push` only when:

- the approved implementation contract exists;
- blocking decisions are resolved;
- every acceptance criterion passed or has an explicitly accepted limitation;
- verification applies to current HEAD;
- independent review applies to current HEAD and has no unresolved blocking finding;
- the worktree is clean;
- the commit stack is inspectable.

Notify the user and generate a copy-pastable ticket/PR blurb from stored evidence:

```text
Verification performed:
- ...
- ...

Limitations:
- ...
```

Do not fabricate evidence from memory.

---

## 12. Artifact freshness and invalidation

Every plan, verification report, and review must record the relevant Git commits.

Examples:

```text
Plan: based on base commit abc123
Verification: tested head def456
Review: reviewed abc123..def456
```

Apply deterministic invalidation rules:

- implementation edit → invalidate verification and review;
- addressing review comments → invalidate verification and review;
- rebase with conflicts or manual resolution → invalidate verification and review;
- clean rebase → at minimum invalidate smoke/integration verification and mark review for policy-based refresh;
- commit-message-only edit → preserve behavioral verification but update lineage;
- pure restack without tree change → compare tree hash and preserve only artifacts whose inputs are unchanged.

Use Git tree/commit information, not model judgment alone, to determine freshness.

---

## 13. Additional supported workflows

### 13.1 Answer a codebase question

- Route to the job's existing contextual worker when relevant.
- Otherwise create a temporary read-only question worker.
- Do not create a worktree unless needed.
- Allow the user to promote the question into a tracked implementation job.

### 13.2 Rebase a stack

- Route to the job's writable worker.
- Apply configured rebase preferences.
- Show base, commit stack, conflicts, and result.
- Never force-push automatically.
- Invalidate artifacts according to the actual tree change.
- Make `smoke-test` and `rereview` easy next actions.

### 13.3 Address review comments

For each comment:

1. inspect the claim and relevant code;
2. classify it as valid, partially valid, invalid, already addressed, or needing human input;
3. fix valid issues or give a concise evidence-based reason for no change;
4. record commit and required re-verification.

```python
class CommentResolution(BaseModel):
    original_comment: str
    classification: Literal[
        "valid", "partially_valid", "invalid",
        "already_addressed", "needs_human_decision"
    ]
    reasoning: str
    action_taken: str | None
    commit: str | None
    verification_required: list[str]
```

After code changes, schedule targeted verification and rereview.

### 13.4 Smoke test / full verify / rereview

These are independently callable blocks. The user can say:

```text
Run another smoke test.
Rereview after the rebase.
Verify only the auth flow again.
```

The manager routes them to the correct job and current HEAD.

### 13.5 Finalize and cleanup

- Present branch, commits, status, verification summary, review summary, and limitations.
- Require user approval before push/merge operations.
- Cleanup only after deterministic safety checks.

---

## 14. Persistence and recovery

On application restart:

- restore registered repositories, jobs, workers, artifacts, decisions, queue state, selected worker, pins, and snoozes;
- resume SDK sessions by stored session ID where possible;
- detect missing worktrees or changed repository state;
- mark unrecoverable workers `disconnected` with an actionable explanation;
- allow a replacement worker to be created from structured job artifacts;
- never silently claim a worker is still running when its process is gone.

The application must remain useful even when a session cannot be resumed because the plan, criteria, decisions, commits, and evidence are externalized.

---

## 15. Suggested package layout

```text
claude-session-manager/
├── pyproject.toml
├── GOAL.md
├── src/
│   └── csm/
│       ├── __init__.py
│       ├── __main__.py
│       ├── app.py
│       ├── config.py
│       ├── domain/
│       │   ├── models.py
│       │   ├── enums.py
│       │   ├── events.py
│       │   └── contracts.py
│       ├── storage/
│       │   ├── database.py
│       │   ├── migrations.py
│       │   └── repositories.py
│       ├── agents/
│       │   ├── backend.py
│       │   ├── sdk_backend.py
│       │   ├── manager.py
│       │   ├── snapshots.py
│       │   └── normalization.py
│       ├── git/
│       │   ├── runner.py
│       │   ├── repositories.py
│       │   ├── worktrees.py
│       │   └── safety.py
│       ├── routing/
│       │   ├── router.py
│       │   ├── validation.py
│       │   └── attention.py
│       ├── workflows/
│       │   ├── registry.py
│       │   ├── plan_feature.py
│       │   ├── implement.py
│       │   ├── verify.py
│       │   ├── review.py
│       │   ├── rebase.py
│       │   ├── review_comments.py
│       │   └── finalize.py
│       └── ui/
│           ├── screens.py
│           ├── manager_pane.py
│           ├── worker_list.py
│           ├── worker_chat.py
│           ├── attention_banner.py
│           └── help.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
└── MVP_EVIDENCE.md
```

Keep business logic out of Textual widgets. UI actions call services; services emit events; persistence and queue updates follow from events.

---

## 16. Implementation order

### Phase 0: prove dependencies

Create small executable spikes proving:

- Textual can stream output from two concurrent async tasks;
- the Agent SDK can maintain two independent interactive worker sessions with different `cwd` values;
- session IDs can be captured and resumed;
- a temporary Git repository can create and remove isolated worktrees safely.

Delete or fold spikes into tests afterward.

### Phase 1: deterministic session manager

Implement:

- repository registration;
- SQLite schema/migrations;
- jobs/workers/events;
- worktree service and invariants;
- SDK worker backend;
- three-pane UI;
- direct worker interaction;
- worker list/status;
- attention queue and auto-advance;
- stop, interrupt, recovery, and safe cleanup.

A deterministic command parser may temporarily stand in for the manager model, but the domain API must be identical.

### Phase 2: manager router

Implement:

- bounded state snapshots;
- manager custom tools;
- natural-language route proposals;
- deterministic route validation;
- create/open/send/stop/cleanup operations through manager requests.

### Phase 3: contracts and workflows

Implement:

- structured implementation, behavior, and evidence contracts;
- plan approval and interactive decisions;
- standard feature loop;
- smoke/full verification;
- fresh independent review;
- rebase, review-comment, question, rereview, and finalize workflows;
- artifact freshness/invalidation;
- ready-to-push verification blurb.

### Phase 4: polish and proof

Implement:

- restart recovery;
- useful errors and logging;
- narrow-terminal behavior;
- configuration/model policy;
- comprehensive tests;
- realistic smoke-test evidence.

---

## 17. Acceptance criteria

### Core application

1. `python -m csm` launches after the documented setup.
2. The UI contains manager, worker list/attention queue, and worker conversation panes.
3. A repository can be registered and persisted.
4. The user can create at least two independent workers in the same repository without sharing a writable worktree.
5. The user can create workers in different repositories.
6. Multiple workers stream concurrently while only the selected worker occupies the right pane.
7. Selecting a worker restores its transcript and allows normal follow-up messages.
8. Workers have independent session IDs, contexts, and working directories.
9. Workers cannot access global registry/manager tools.
10. Application restart restores durable state and resumes or clearly marks sessions that cannot be resumed.

### Manager and routing

11. The manager can create, list, inspect, open, message, interrupt, stop, and request cleanup of workers.
12. “Rebase this,” “run another smoke test,” and “rereview it” route to the selected/current job and correct workflow.
13. An unrelated request creates a new job/worker rather than polluting an existing worker.
14. Ambiguous destructive operations require clarification or confirmation.
15. Manager context is bounded and built from structured state; completed/history data is not loaded indiscriminately.
16. Pasting a complete ticket into the single manager input creates or routes to the correct job without requiring a separate ticket form or workflow selector.
17. Ticket intake extracts an identifier/title when available, resolves the repository, selects an appropriate workflow bundle, and starts the necessary independent worker(s); it asks at most one concise clarification when harmful ambiguity remains.
18. Pasting a ticket already represented by an active job routes to that job rather than silently creating a duplicate.

### Attention workflow

19. One worker can become blocked while another continues working.
20. The blocked worker appears at the top of the attention queue with a concise reason.
21. After the user responds and the worker resumes, the next actionable worker opens automatically.
22. Auto-advance can be paused; workers can be pinned and snoozed.
23. The UI never auto-switches while the user is typing.

### Git/worktree safety

24. No two writable workers can own the same worktree.
25. Review/question workers are read-only by default.
26. Unsafe cleanup is refused without losing work and explains why.
27. Safe cleanup stops the worker and removes only safe session/worktree state.
28. No force-push, merge, branch deletion, or discard occurs without explicit approval.

### Contracts and feature loop

29. A feature request produces a main plan of at most 10 lines plus structured decisions, acceptance criteria, evidence requirements, risks, and commit stack.
30. Material decisions appear as concrete choices with a recommendation.
31. The approved contracts can seed a separate implementation worker without sharing the planner transcript.
32. Verification records criterion-specific evidence tied to current HEAD.
33. A fresh independent reviewer receives contracts, diff, commits, and evidence but not implementer reasoning.
34. Code changes deterministically invalidate stale review/verification.
35. A job cannot reach `ready_to_push` with unresolved blocking findings, stale evidence, or unmet criteria.
36. The final notification contains a copy-pastable verification blurb generated from stored evidence and honest limitations.

### Other workflows

37. `rebase-stack` follows configured preferences and does not force-push.
38. `address-review-comments` classifies every comment and fixes or explains it.
39. `smoke-test`, `full-verify`, and `rereview` can be invoked independently after a rebase or fix.
40. `answer-codebase-question` can run read-only without creating an unnecessary writable worktree.

### Quality

41. Unit tests cover routing, attention priority, state transitions, snapshot bounding, artifact invalidation, and cleanup safety.
42. Integration tests cover repository registration, worktree creation/ownership, persistence/recovery, and cleanup with temporary Git repositories.
43. Tests cover at least one full mocked feature workflow from plan through ready-to-push.
44. The complete test suite passes.
45. Type hints are used throughout and subprocess failures surface actionable errors.
46. Manager and worker prompt templates enforce concise, plain-English user-facing output without reducing available reasoning or tool capabilities.
47. Representative manager, planner, worker, verifier, and reviewer responses are concise and do not repeat known context.
48. The implementation itself is delivered as a coherent atomic commit stack, and the final evidence lists each commit and its purpose.
49. The implementation process uses bounded subagents where useful and includes a fresh independent final review; subagent use must not introduce overlapping writable file ownership.
50. Before substantial implementation, the implementation agent records concise implementation, behavior/acceptance, and evidence contracts for this prototype and verifies completion against them.

---

## 18. Required evidence before completion

Create `MVP_EVIDENCE.md` containing:

- exact environment/setup commands;
- exact test and launch commands;
- command exit codes and test results;
- the concise implementation, behavior/acceptance, and evidence contracts used to build this prototype;
- the final atomic commit stack, with one-line purpose per commit;
- a concise record of subagents used, their bounded scopes, and how their outputs were verified;
- the fresh independent final review, its findings, and how valid findings were resolved;
- an acceptance-criteria checklist;
- a demonstrated scenario with two independent workers in separate worktrees;
- a demonstrated blocked worker while another continues;
- response followed by attention auto-advance;
- a sample concise implementation/behavior/evidence contract;
- a sample independent review and artifact invalidation after a code change;
- `git worktree list` output from the scenario;
- screenshots or terminal captures of the three-pane UI;
- known limitations and external blockers;
- deliberately deferred non-goals.

Do not claim success without this evidence.

---

## 19. Usage scenarios the finished prototype must support

### Start a feature by pasting a ticket

```text
User → Manager: <pastes the full ENG-421 ticket into the ordinary manager input>
Manager → extracts the ticket identity, resolves the repository and existing related work, selects the contract-driven feature workflow, and creates the required job/worker(s).
Planner → receives the ticket in an independent read-only session.
Planner → concise plan, decisions, criteria, evidence, commit stack.
User → approves/answers.
Manager → starts implementation worker in isolated worktree.
Manager → later starts verifier and independent reviewer.
User → is notified when ready to push and receives verification blurb.
```

### Route to an existing worker

```text
User → Manager: Rebase this stack.
Manager → resolves selected job and writable worker.
Worker → executes rebase-stack using saved preferences.
System → invalidates stale evidence as required.
Manager → offers/runs smoke-test and rereview.
```

### Process review comments

```text
User → Manager: Address these review comments: <comments>.
Manager → routes to implementation worker with address-review-comments.
Worker → classifies each comment, fixes valid items, explains invalid ones.
System → reruns required verification and fresh review.
```

### Work through the attention inbox

```text
Auth worker needs compatibility decision.
Planner needs plan approval.
Cache change is ready to push.

User answers Auth worker.
→ it resumes
→ Planner opens automatically
User approves plan.
→ Cache change opens automatically
```

### Ask a question

```text
User → Manager: Is this cache shared between requests?
Manager → routes to the contextual job worker or creates a read-only question worker.
User → chats with that worker directly in the right pane.
```

---

## 20. Definition of done

The goal is complete when the user can run one Python application, paste a ticket or any other coding request into one universal manager input, and use it as a reliable personal control plane for multiple independent Claude coding sessions:

- no manual terminal juggling;
- no manual worktree bookkeeping;
- clear visibility into running and blocked sessions;
- direct interaction with every worker;
- automatic movement through the attention queue;
- natural-language routing to existing or new workers;
- reusable planning, implementation, verification, review, rebase, and review-comment workflows;
- concise, plain-English communication with detail on demand;
- concise upfront agreement on implementation shape, behavior, and evidence;
- bounded subagent delegation and atomic implementation commits;
- deterministic safety and artifact freshness;
- durable state and safe cleanup;
- tests and evidence demonstrating the complete workflow.

Optimize for **reducing coordination burden**, not maximizing the number of agents.
