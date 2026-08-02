# How Switchboard got here

The rest of `docs/` describes the system as it is. This one records how it got that way and
why, because several of the decisions look arbitrary until you know what they replaced.

It is a narrative, not a changelog. Commit hashes and dates appear where they anchor a
decision; `git log` has the rest.

---

## 1. The idea

**`dd39f29`, 2026-07-31.** One specification, written before any code: a control plane for
multiple independent Claude coding sessions. The problem it named was not that agents are
hard to launch. It was that running several of them turns the human into a process manager
— opening terminals, finding worktrees, remembering which window is which ticket, and
checking every session to see which one is blocked.

Three ideas from that document survived everything that followed: **jobs and workers are
different things**, **worktree safety belongs to the application rather than the agent**,
and **a change is finished when stored evidence says so, not when a model says so**.

The original specification is still recoverable: `git show
dd39f29:CLAUDE_SESSION_MANAGER_GOAL.md`. It is deliberately not kept in `docs/`, because
most of it now describes a design that two migrations replaced.

## 2. The first build: SDK workers, a rule-based manager

**`b96c2fe` … `7464aa6`, 2026-07-31.** Domain models, SQLite, the Git runner and worktree
service, a `WorkerBackend` protocol with an Agent-SDK implementation and a deterministic
scripted one, the workflow registry, `SessionManager`, the router and attention queue, a
constrained manager tool surface, and a three-pane Textual UI.

The `WorkerBackend` protocol and the scripted backend beside it turned out to be the two
most valuable decisions in the whole project, for a reason nobody anticipated: they made a
complete substrate replacement possible later without touching orchestration, and they made
the entire control plane testable with no model calls at all.

The prerequisite gate arrived here too (`7e5b562`), and immediately earned itself — the
first real model run tried to skip planning and go straight to implementation, and was
refused.

## 3. Workflows became data

**`d73b4a2`, `72dad89`, 2026-08-01.** Workflows moved out of Python and into YAML, and a
composite workflow became an ordered list of steps over other workflows. `complete-ticket`
stopped being special code and became a composite of the same atomic workflows a user can
invoke directly.

That is what made the development ritual itself configurable, and it is why
`~/.switchboard/workflows` and a repository's `.switchboard/workflows` need no core change.
It also created the first real safety question — a workflow file inside a repository could
name `implement-approved-plan` and quietly declare that it requires nothing — answered by
making built-in names reserved (`b009ec3`).

**`8c93306`, `63aba32`:** the project became Switchboard and the command became `sb`.

## 4. The pivot: native Claude is the interaction surface

This is the decision that reshaped everything.

The SDK build owned the conversation. It rendered worker output in its own pane and offered
`claude --resume` when the user wanted to intervene. That was fine right up to the moment
you actually wanted to intervene, and then it was not: a resumed session is a *different*
process with a different tool policy, and it cannot rejoin a turn already in flight. The
product's central promise — *step into any live session, work normally, leave, and let the
larger workflow continue* — was not deliverable on top of a replacement process.

The realisation was that Claude Code was already the interaction surface, and Switchboard
was reimplementing a worse version of it. Everything below follows from taking that
seriously.

### Durable runtimes and generations
**`b060b9f`, `08fb179`, `5f4605c`.** If the process outlives the controller, the controller
needs durable identity for it. `RuntimeInstance` records substrate-neutral process state,
generation, input ownership, Claude session identity, a launch fingerprint, and the Git
baseline of an active writable turn. Recovery adopts by *exact* generation or refuses; it
never guesses.

### tmux as the substrate
**`23429d0`, `db73d53`.** One dedicated tmux server, one session per runtime generation —
not windows in a shared session, so each process gets its own attach target, client count,
ownership metadata, exit state and cleanup boundary. Prompt bytes go through
`load-buffer`/`paste-buffer`, never as command arguments.

The rule that kept this honest: **this layer observes process lifetime and ownership only.**
It never reads pane contents to decide whether Claude is ready, blocked, or finished. A
live pane stays `STARTING` until something semantic says otherwise.

### Hooks as the semantic layer, and turn provenance
**`9857c71`, `1848a21`, `605ab6e`.** Claude's command hooks supply the semantics tmux
refuses to invent. `SessionStart` means ready; `Stop` carries the result;
`PermissionRequest` means waiting.

The subtle part is provenance. A human can type into the same session at any moment, so a
turn carries a durable origin. Switchboard writes a `PENDING` turn with a 256-bit token,
appends it to the injected prompt, and binds Claude's own `prompt_id` to it at
`UserPromptSubmit`. **Only a `MANAGED` turn no human touched may harvest an artifact or
advance a workflow.** Everything else is recorded and inert.

`605ab6e` added the delivery ledger, because command hooks are subprocess callbacks and not
a durable event transport: a replay after a controller restart must not duplicate a
transcript row, an artifact, or a step advancement.

### Native workers become production
**`fc8e0dd`, `4bc5468`, 2026-08-01.** The SDK backend was removed rather than kept as a
fallback. Two backends with different semantics would have meant every invariant needed
proving twice, and the fallback would have quietly become the tested path.

## 5. Composite runs on real sessions

**`93367c0` … `2270f6a`.** Composites over durable native processes forced the exactly-once
question into the open, and the answer is the most safety-critical rule in the codebase:

> Reserving a worker and sending it a prompt do not complete a step. Only a successfully
> applied, manager-owned terminal event does, and the completion marker shares a transaction
> with artifact harvesting.

Everything else follows: recovery advances a marked step once; loss of an incomplete runtime
blocks rather than resends; a failed turn never grants authority; human intervention taints
the attempt and requires an explicit resume, which replays the same bounded step from the
durable contracts without consuming an iteration.

`a62e559` is where "human intervention taints the attempt" became real, and Phase 8 later
showed that this correct rule has an uncomfortable consequence — see §8.

**Authoritative lineage** landed here too. A job may have several writable workers, but
exactly one worktree *is* the change. Without that, a reviewer could be handed a different
writable worker's tree and report on code nobody asked about.

## 6. The Manager becomes native too

**`8a3417b` … `8c9bf02`.** The manager was the last SDK component. Making it a native Claude
process on the same substrate meant the user could enter it exactly like a worker — but it
also meant it needed tools, and giving a model orchestration authority is a security
boundary, not a convenience.

The answers: a **manager-only MCP** over a mode-0600, generation-specific Unix socket to the
board's authoritative `SessionManager`; **generation-bound authority**, revalidated on every
single call, so an inherited pipe is not enough; `--strict-mcp-config` so no arbitrary
user or project MCP can reach it; and workers that never receive its configuration, socket,
or launch arguments at all.

Context handling followed from the same principle as everything else: SQLite is long-term
memory, the transcript is working memory. Rotation keeps at most 4,000 characters of
handoff, and a fresh generation reconstructs jobs, runs, workers, attention, contracts, and
decisions through its own tools. **Losing the Manager transcript cannot change workflow
correctness.**

## 7. Session-first UI, and the first real Claude runs

**`91dce71`.** The board stopped being a chat client. Manager and workers share one session
list; the detail pane shows durable orchestration state — workflow, lifecycle, ownership,
blocker, worktree, lineage, run step, evidence — and Enter opens the exact native process.
Claude owns conversation rendering; Switchboard owns everything around it.

The first authenticated runs against real Claude Code 2.1.220 immediately found what no
deterministic test could: a 31-second `SessionStart` on first use against a hard-coded
30-second timeout; macOS Unix-socket path limits under a nested `SB_HOME`; first-use trust
prompts that looked exactly like a hung worker; and a Manager launch mismatch that could
mint a peer generation.

## 8. Adversarial dogfooding, and the self-hosting experiment

**Phase 8, 2026-08-01 → 08-02.** Two sessions of using Switchboard on real work, recording
findings from user-visible behaviour before looking at any code. The full record is
[dogfood-report.md](dogfood-report.md).

The suite was green at 362 tests going in, and **every P1 found was live-only**: a dead MCP
bridge after a controller restart, an erased tmux target, a missing event pump, a silently
raised approval gate, a self-fulfilling not-ready refusal. None were testable in isolation
because each depended on a real subprocess lifecycle crossing a real controller restart. The
tests were not bad; they tested the pieces, and the bugs lived between them.

Then Switchboard was pointed at a disposable clone of itself, and asked — in plain English —
to add the `sb runtimes` command that the dogfooding had just proved was missing. What
happened is the most useful result in the project:

- the Manager resolved the repository by name, created the job, and started the composite;
- the planner used one of Claude's own subagents entirely on its own initiative, inside a
  managed session, with Switchboard none the wiser and none the worse;
- and then it asked a genuinely good design question through Claude's question UI — with
  only the five requested fields, two runtime rows would be indistinguishable, so should the
  listing carry a short agent id? It recommended yes. That was a better specification than
  the one it had been given.
- **Answering that question required entering the session, which tainted the attempt, so the
  plan and the contract it had produced were discarded.**

The task therefore produced a real design improvement and no committed implementation. That
is a finding about Switchboard, not a failure to try, and it was deliberately not worked
around by writing the feature by hand. The same composite in an ordinary repository ran end
to end: a real edit, a real test, a real commit in an isolated worktree, and a passing
verification report, with the user never typing code.

The honest summary of what dogfooding showed: the job/worker/worktree graph and durable
adoption across restarts are genuinely valuable; approval gates enforced in Python rather
than by asking a model nicely are genuinely valuable; and **native prompts dominate the
loop** — every new repository and worktree costs a trust prompt, the first `Edit` and `Bash`
cost permission prompts, and entering to answer them is expensive because it taints the
attempt. The two costs compound. The supervise-without-entering-terminals promise is
undermined by the entering being mandatory.

## 9. Phase 10: the cleanup this document belongs to

**2026-08-02.** The repository at that point read like what it was — an SDK design with a
native architecture grown over it. Dead migration gates (`supports_composites` was `True` in
every backend and guarded six unreachable branches), two vocabularies for the same workflow
fields, a "profile" that was a composite workflow under another name, configuration nothing
read, and ten documents describing six different moments of the design.

No architecture was redesigned. Git lineage and the ready-to-push gate moved out of
`SessionManager` because they were pure functions of the store that never needed the
orchestrator; the tests were sorted into tiers by what each can actually prove; and `docs/`
became a description of the finished system with this document holding the history.

---

## What changed our mind, and when

| Belief | What replaced it | Because |
| --- | --- | --- |
| Switchboard renders the conversation | Claude Code owns the conversation | A resumed session is a different process; you cannot step into a live turn |
| The manager holds the state | SQLite holds the state; the manager is replaceable | A transcript that can be lost must not be able to change correctness |
| Terminal output tells you what the agent is doing | Hooks tell you; tmux only tells you the process is alive | Screen-scraping an interactive TUI is a guess dressed as a fact |
| A worker finishing means a step finished | Only a trusted managed terminal event means it | Assignment, delivery, and completion are three different things |
| Two backends give you a safety net | One backend, with a deterministic test double | A fallback quietly becomes the tested path |
| Tests prove integration | Tests prove the pieces; dogfooding proves the seams | Every Phase 8 P1 was invisible to a green 362-test suite |

---

# How Switchboard was built

Switchboard was written almost entirely by AI agents under human product direction. That is
worth recording carefully rather than triumphantly, because the interesting part is the
*process*, and because a single project cannot establish very much on its own.

## The process

```
human product and architecture direction
  → ChatGPT as an independent planner and reviewer
  → Codex as the repo-local implementation agent
  → implementation in bounded phases
  → tests and atomic commits
  → independent review from a fresh context
  → dogfooding against real Claude
  → Switchboard coordinating Claude agents working on Switchboard
```

Five things were load-bearing:

1. **The human owned product and architecture decisions.** Every pivot in the first half of
   this document — native processes, tmux, hooks, one backend not two — was a human call.
   Agents executed and argued; they did not choose the shape of the system.
2. **The planner and the implementer were different agents in different contexts.** Plans
   were written where the code was not.
3. **Work was bounded into phases with observable acceptance criteria.** A phase had a
   stated end state and evidence it was reached.
4. **Review came from an agent that had not written the code and did not have its context.**
5. **Commits were atomic and the test gate was executable.** A fix had to be shown to fail
   without its change.

## What the evidence from this project suggests

**Fresh-context review found what the implementer could not.** The Phase 8 independent
review returned four important findings, all in code written hours earlier by the agent that
had just tested it. One of them mattered a great deal: the widened plan-approval matcher
granted approval on *"The plan looks good. Do not implement until I have spoken to Sam."* —
sentence-scoped negation meant punctuation decided whether an explicit refusal counted. The
agent that wrote it had also written its tests, and those tests were tautological: every
withheld case contained a literal stop-word, so the suite restated the keyword list rather
than probing it.

**Reproduce-first discipline was worth more than any tool.** Twice, inspecting internals
first would have produced the wrong fix. A stray `start it` in a composer turned out to be
Claude Code's own ghost-text suggestion; a "broken" `Ctrl+Space` turned out to be a missing
key mapping in the test harness. Both were dismissed by evidence rather than patched.

**Executable validation caught tests that proved nothing.** Reverting each fix and rerunning
its specific test caught one test that passed either way, and one code placement that was
pure speculation and was deleted rather than committed. The clearest example: a regression
test for the approval-gate bug passed against the buggy code, because the run still had a
`current_worker_id` and never reached the broken fallback. It only became evidence once the
test set that field to `None` — the state the real path is reached in.

**Written durable state substituted for continuity.** Context is lost constantly in this
mode of working, so the phase documents and `CLAUDE.md` carried the decisions and the commits
carried mechanical changes against them. A cold implementation agent could reconstruct intent
without any chat history. This is the ordinary benefit of a written design doc, obtained
under conditions where nothing else preserves intent.

**Dogfooding found a category of bug testing did not.** Stated once more because it is the
single strongest result here: 362 green tests, and every P1 found by using the thing was a
state-reconciliation gap across a real subprocess lifecycle and a real controller restart.

**And the honest limit:** an agent dogfooding its own project is not an independent user. It
knows where to look, tolerates friction a real user would not, and is tempted to explain away
rough edges. The friction findings in the dogfood report are the ones most likely to be
understated.

## How that lines up with what is known more broadly

These are external results. They are consistent with what happened here; they are not
evidence *from* here, and none of them was gathered on a project like this one.

**On separating production from review.** A March 2026 study of "Cross-Context Review"
tested exactly this pattern — an LLM reviewing an artifact in a fresh session with no access
to the production conversation — against same-session self-review across 360 reviews with
injected errors. Cross-context review beat self-review on F1 (28.6% vs 24.6%, p = 0.008) and
detected 40% of critical errors against self-review's 29%. Notably, reviewing *twice* in the
same context was no better than once, which points at separation rather than repetition as
the mechanism ([arXiv:2603.12123](https://arxiv.org/html/2603.12123)). That matches this
project's experience closely enough to be worth naming — though a single external study with
those effect sizes is a directional result, not a settled one.

Related work on LLMs as reviewers is less flattering to the general idea: self-enhancement
bias, in which a model rates its own output more favourably than another model's, is a
documented failure mode of LLM-as-judge setups
([arXiv:2412.05579](https://arxiv.org/pdf/2412.05579)), and LLM code reviewers show
systematic overcorrection when judging conformance to a requirement
([arXiv:2603.00539](https://arxiv.org/html/2603.00539v1)). A fresh reviewer is a mitigation,
not a solution; on this project the reviewer's findings were still triaged by a human and one
of its suggestions was narrowed as over-broad.

**On bounded work.** The classic result is the Cisco/SmartBear code-review study — 2,500
reviews over 3.2 million lines — which found defect detection best in the 200–400 line range
and falling off sharply beyond it, with review rates above ~450 lines/hour producing
below-average defect density in 87% of cases
([SmartBear/Cisco case study](https://static0.smartbear.co/support/media/resources/cc/book/code-review-cisco-case-study.pdf)).
That is a finding about human reviewers, and it should not be extrapolated to model reviewers
without evidence. It is, however, the reason bounded phases and atomic commits are not merely
tidy: they keep each review inside the range where review is known to work at all.

**On AI as an amplifier rather than a multiplier.** DORA's 2025 *State of AI-assisted
Software Development*, across roughly 5,000 professionals, found that AI adoption now
correlates positively with delivery throughput *and* continues to correlate negatively with
delivery stability — more change failures and more rework — and frames AI's primary role as
"an amplifier, magnifying an organization's existing strengths and weaknesses"
([dora.dev](https://dora.dev/research/2025/dora-report/)). The mechanism DORA describes is
exactly what this project ran into: acceleration exposes downstream bottlenecks in testing,
review, and quality assurance. Switchboard's entire premise — contracts, evidence gates,
independent review, deterministic freshness — is an attempt to build that control system
*into* the loop rather than leave it downstream.

**And the strongest counterweight.** METR's randomized controlled trial of 16 experienced
open-source developers on 246 real tasks in their own repositories found that allowing AI
tooling made them **19% slower**, while the same developers estimated afterwards that it had
made them 20% faster — a roughly 40-point gap between perceived and measured effect, in the
direction that should worry anyone writing a document like this one
([arXiv:2507.09089](https://arxiv.org/abs/2507.09089)). The study used early-2025 tooling on
mature codebases the developers knew deeply, which is close to the opposite of this project's
conditions (a greenfield repository, an unfamiliar-to-everyone design). It is not a refutation
of anything here. It is a reason to distrust any claim in this section that rests on how
productive the process *felt*.

## What this project cannot tell you

- **Whether it was faster.** Nothing was measured against a control. No baseline exists, and
  METR's result is a direct warning about substituting impressions for measurement.
- **Whether the design is good.** It was dogfooded by its own author-agent, which is the
  weakest possible form of user testing.
- **Whether any of this generalises.** One greenfield Python project, one developer, one
  toolchain, a few days. Every claim above is an observation, not a finding.
- **Whether the process or the models did the work.** These cannot be separated here. A
  weaker model with the same discipline was never tried, nor a stronger one without it.

## What is worth carrying forward anyway

Stated as hypotheses, because that is what they are:

- Separate the agent that plans from the agent that implements, and the agent that reviews
  from both.
- Bound the unit of work so that a review fits inside the size where review works.
- Make the gate executable. "Tests pass" is not evidence of the behaviour the request was
  about; reverting the change and watching the test fail is.
- Write durable state — design docs, contracts, commit messages — as though every context
  will be lost, because it will be.
- Keep product and architecture decisions with the human. Every pivot in this project was
  one, and none of them was a coding problem.
- Dogfood, and expect it to find a different class of bug than the suite does. It did here,
  every time.
- Treat AI as an amplifier of the engineering process you already have. That framing is
  DORA's, it is the one this project's evidence fits, and it implies the process is the
  variable worth investing in.
