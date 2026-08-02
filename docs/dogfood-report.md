# Adversarial dogfood report — August 2026

> **A dated field record, not current documentation.** This is what happened when
> Switchboard was used adversarially on real work, written at the time. Findings are
> recorded from public, user-facing behaviour *before* any implementation inspection or
> repair; diagnosis, resolution, regression coverage, and replay results were appended
> only after the original reproduction existed. Fixes named here have since landed. Its
> own limitations section is frozen as written; the maintained list, which is a superset,
> is [troubleshooting.md](troubleshooting.md#known-limitations).
>
> It is kept because the failures are the evidence. The narrative of how the architecture
> got here is in [project-evolution.md](project-evolution.md).

## Test environment

- Stable runtime: archived source at commit `05176e7`, launched with the provisioned
  development interpreter and `PYTHONPATH` pointed at the archive.
- State: isolated under `/tmp/switchboard-phase8.zxh2GV/state`.
- Targets: disposable `tiny-app` repository and disposable `switchboard-clone`; the stable
  checkout is never a managed target.
- Native Claude: Claude Code 2.1.220; Haiku is the default for paid dogfood turns.

## Findings

### Manager configuration mismatch recovery is slow and initially truncated

Severity: P2

Category: recovery / UX / native integration

Scenario:
Change the supported Manager model from Haiku to Sonnet while its native process remains alive,
then restart the board and follow the product's fresh-Manager recovery path.

Steps to reproduce:
1. Start and trust a Haiku Manager, then stop only the controller.
2. Change `models.manager` to `sonnet` in documented configuration.
3. Restart `sb` against the same state.
4. Submit `fresh manager` in the board composer.

Expected:
The mismatch fails closed with a complete actionable explanation, and explicit fresh Manager
rotation starts a matching Sonnet generation.

Actual:
The board remains `ready · manager` for the mismatched runtime. Its visible message is truncated to
`Recovery failed: The live Manager process does not match the current`, omitting the remedy.
Submitting `fresh manager` initially appears to do nothing. After the native startup delay, the
board changes to `starting` and eventually shows the replacement Sonnet generation as ready.

Evidence:
The Manager pane rendered only two wrapped recovery lines. A longer observation showed generation
rotation did succeed, but only after roughly a minute with no early visible acknowledgement.

Reproducibility: Once.

Workaround: Submit `fresh manager` and wait through the full native startup boundary.

Diagnosis:
The mismatch itself failed closed correctly; what failed was visibility. The Manager pane painted
entries oldest-first into a short fixed-height region, so a long recovery message was clipped before
its remedy. The unacknowledged minute is the cost of launching a real native Claude process, which
Switchboard cannot make faster.

Resolution:
The Manager pane paints newest-first and the current goal is compacted, so the recovery message and
the latest outcome own the visible rows (`d5b8f6e` and the compact-goal fix). Rotation latency was
deliberately not changed. A single message longer than the pane is still clipped at the bottom;
`#manager-log` has no scrollback.

Regression coverage:
The 80x24 UI tests assert the latest Manager outcome stays visible after an older note.

Replay result:
Latest objective/status visibility passed after controller restart. The mismatch scenario itself was
not re-run live. Note for the reader: this slot previously held the recovery/attention resolution
text belonging to the stale-native-attention finding below — a copy-paste error at the time of
writing, corrected at closeout rather than left to imply a fix this finding never had.

### Fresh Manager rotation leaves the submitted command in the composer (rejected)

Severity: P2

Category: UX / native integration

Scenario:
Rotate to a fresh Manager from the normal board composer, then continue with another goal.

Steps to reproduce:
1. With a ready Manager selected, type `fresh manager` and press Enter.
2. Wait for the replacement generation to become ready.

Expected:
The submitted command clears just like any other completed Manager message.

Actual:
The board reports `Started fresh Manager generation 2`, but `fresh manager` remains in the
composer with the cursor at its end. Pressing Enter again would repeat the rotation.

Evidence:
The 80x24 PTY capture shows generation 2 ready while row 10 still contains the focused
`fresh manager` value.

Reproducibility: Once; not reproducible through ordinary separated submissions.

Workaround:
Use the input's line-edit command to clear the stale value before typing another request.

Diagnosis:
Rejected as a product defect. The reproduction injected the handback answer and next command in
one low-level PTY write across Textual's terminal-mode handoff. Normal separated submissions clear
immediately, including after exact-session entry and controller recovery.

Resolution: No product change.

Regression coverage: Not applicable; nothing was changed.

Replay result: Not applicable; the reproduction itself was rejected as an artefact of the harness.

### Startup trust instruction conflicts with focused Manager composer

Severity: P2

Category: UX / native integration

Scenario:
A new engineer launched `sb --register <fresh-repo>` with a fresh `SB_HOME`. Native Manager
startup stopped at Claude's trust prompt and the board selected Manager.

Steps to reproduce:
1. Launch `sb --register <repo>` with fresh isolated state and the native backend.
2. Wait for Manager to show `starting` and the detail instruction `Press Enter to handle
   workspace trust, login, or another startup prompt.`
3. Press Enter exactly as instructed.

Expected:
The exact Manager process opens so the native trust prompt can be handled.

Actual:
The Manager composer retains focus and Enter submits its empty contents. The board remains in
place. `Ctrl+E` opens the exact process, but the visible instruction does not say that focus
changes the meaning of Enter.

Evidence:
The PTY capture remained on the board after repeated Enter presses; `Ctrl+E` immediately opened
Claude Code's `Accessing workspace: .../manager-workspace` trust screen in the tmux target.

Reproducibility: Always on this fresh-state launch.

Workaround:
Press `Ctrl+E` instead of Enter.

Diagnosis:
The startup note named Enter, but Enter belongs to whatever widget holds focus; on the board that is
the Manager composer, so the documented remedy submitted an empty message instead of entering the
session.

Resolution:
The startup note now tells the user to press `Ctrl+E`, whose meaning does not depend on composer
focus.

Regression coverage:
The UI integration test asserts the native startup guidance names `Ctrl+E` rather than Enter.

Replay result:
Passed in two fresh native workspaces: the board displayed the corrected instruction, and
`Ctrl+E` opened the exact Claude trust process.

### Repeated Manager entry terminates the board on the second handback

Severity: P1

Category: correctness / native integration / recovery

Scenario:
Enter and leave the same native Manager repeatedly, including once after controller recovery.

Steps to reproduce:
1. Launch `sb` with the isolated native Manager already alive and human-owned.
2. Press `Ctrl+E`, detach with `Ctrl+B`, `d`, answer `yes`, and observe the board recover.
3. Press `Ctrl+E` again and detach the same way.

Expected:
The board returns with Manager `ready · manager`.

Actual:
The second detach prints tmux's normal `[detached ...]` line, but no handback question appears and
the `sb` controller process exits. The exact native Manager remains alive.

Evidence:
On the clean replay, the first attach displayed
`Confirm Claude's composer is empty before manager handback [y/N]:`; `yes` restored
`ready · manager`. After a Manager turn, the second attach reached the same native session and
detached normally, but `ps` then found no controller and still found the same Claude/tmux runtime.
The configured log contained no diagnostic.

Reproducibility: Twice across the first-startup run and the clean repeated-entry replay. A single
ready-Manager attach/handback succeeds.

Workaround:
Restart `sb`; the native process remains available for recovery.

Diagnosis:
`action_attach` awaited both `subprocess.run` and `input` through executor threads while inside
Textual's synchronous `App.suspend()` context. A one-second stack sample of the hung controller
showed the main thread blocked resuming application mode while the executor worker was already
idle: neither tmux nor the confirmation read remained active. Textual documents this context for
synchronous terminal handoff; spanning it with asynchronous yields creates a resume race.

Resolution:
Keep tmux attachment and handback confirmation synchronous inside the terminal suspension context,
so ownership transfer and application-mode restoration cannot interleave with Textual tasks.

Regression coverage:
The UI handback integration test now performs two consecutive exact-session attach/release cycles.

Replay result:
Passed in a fresh isolated native state. After accepting Manager workspace trust and waiting for
the delayed SessionStart, two consecutive `Ctrl+E` → tmux detach → `yes` cycles each displayed the
confirmation immediately and returned to `ready · manager` with the same Claude session ID.

### A normal-length goal hides the Manager's outcome at default terminal size

Severity: P2

Category: UX / observability

Scenario:
Submit a small natural-language implementation goal in the default 80x24 board and wait for the
Manager turn to finish.

Steps to reproduce:
1. Launch `sb` in an 80x24 terminal with one repository.
2. Submit the tiny-app greeting goal from the previous finding (111 characters).
3. Wait for Manager to return to `ready`.

Expected:
The Manager's outcome or failure is visible in the Manager pane.

Actual:
`Current goal` wraps across four rows and consumes the small top-left pane. The Manager log has
no visible row, so the outcome is hidden. Entering the exact Manager is required to discover
what it claimed.

Evidence:
The PTY capture shows rows 5-8 occupied by the wrapped current goal and rows 10-11 occupied by
the composer; no response text is visible after the turn completes.

Reproducibility: Always at 80x24 with this goal.

Workaround:
Enter the exact Manager session and inspect its native transcript, or use a taller terminal.

Diagnosis:
The goal label allowed 120 visible characters and wrapped inside a six-row pane, leaving no room
for the latest Manager outcome at 80x24.

Resolution:
Compact the current goal to 32 characters so the outcome remains visible in a narrow terminal.

Regression coverage:
The 80x24 UI test submits a normal-length goal and asserts that a subsequent Manager outcome is
visible.

Replay result:
Passed in the native 80x24 board: the goal now occupies two rows and Manager status remains
visible. A separate finding below covers status content that is itself misleading.

### Apparent missing handback after exact-session detach (rejected)

Severity: P3

Category: correctness / native integration / UX

Scenario:
From a normally launched `sb` in an ordinary terminal, enter the exact native Manager to handle
first-use trust, then leave it and return to orchestration.

Steps to reproduce:
1. Launch `sb` with native backend and fresh isolated state.
2. Select Manager and press `Ctrl+E`.
3. Accept Claude's native workspace trust prompt.
4. Detach from the exact tmux session with tmux's normal `Ctrl+B`, `d`.

Expected:
Return to the board, confirm the composer is empty, and hand control back to Switchboard.

Actual:
On the first observation, the terminal printed `[detached ...]` and appeared blank long enough
that the controller was restarted. The restart correctly adopted Manager as `ready · human`.

Evidence:
The exact native screen showed Claude Code 2.1.220, Haiku 4.5, the isolated Manager workspace,
and `manual mode on`. After detach, the original PTY produced no board output. A new public `sb`
launch showed the sole session as `Manager ready · human`.

Reproducibility: Once.

Workaround:
Wait for the terminal handback prompt after detaching.

Diagnosis:
Rejected as a product defect. A focused replay showed the prompt immediately after tmux's
detach line: `Confirm Claude's composer is empty before manager handback [y/N]:`. Answering
`yes` returned to the board and restored `ready · manager`. The original PTY observation missed
the prompt because of capture/timing while the terminal modes were changing.

Resolution: No product change.

Regression coverage: Existing UI coverage already asserts handback after confirmation.

Replay result: Passed through the public UI with the same native Manager process.

### Manager claims a workflow started after emitting unusable placeholder tool calls

Severity: P1

Category: workflow / native integration / observability

Scenario:
Ask the authenticated Haiku Manager to make a tiny deterministic change in the sole registered
repository using an explicitly named built-in workflow.

Steps to reproduce:
1. Start `sb` with the trusted native Manager and one registered `tiny-app` repository.
2. Submit: `In tiny-app, change greeting so an empty name returns Hello, there! and add a test.
   Use the lightweight-feature workflow.`
3. Wait until Manager returns from `turn active` to `ready`.

Expected:
A job and worker appear, or Manager gives an actionable refusal/error.

Actual:
The board remains at one Manager session, zero workers, and `Nothing needs you`. Entering the
exact Manager reveals it printed pseudo tool markup containing
`job_id={{job_id_from_create}}`, then stated `Starting lightweight-feature workflow...` even
though no job or worker exists. The board offers no indication that the claimed action failed.

Evidence:
The native transcript contains literal `<invoke name="switchboard_start_workflow">` markup and
the unresolved placeholder. The public board contains no job/worker after the completed turn.

Reproducibility: Always in two authenticated Haiku Manager turns. The exact failure wording
varied, but both turns ended with zero jobs and workers after claiming coordination was underway.

Workaround:
Inspect the native Manager transcript, notice the mismatch, and ask again without relying on the
claimed result.

Diagnosis:
The first turn emitted invented pseudo tool markup and a placeholder job ID. A first prompt guard
prevented that exact output, but the replay still stopped after saying `Let me register the
repository and create the job:`. It had successfully inspected the workflow, then ended its turn
before performing the next mutation. This is a model-following failure exposed by an orchestration
prompt that permits progress narration and does not explicitly require completing the route before
ending the turn. A fresh-generation replay with the stronger contract no longer claimed success:
it asked `Where is tiny-app located?` even though tiny-app was already the sole registered
repository. The policy tells Manager when it may register a path but not to resolve an already
registered repository by its user-visible name. After that policy was corrected, the same question
persisted. Implementation inspection then found the hard blocker: `inspect_state` omits
repositories, and there is no repository-list tool, so a native Manager cannot discover the ID or
canonical path of a repository registered through `sb --register`. The board compounds all variants
by retaining an older exact-entry status message instead of surfacing the Manager's latest native
outcome.

Resolution:
The Manager contract now forbids invented or premature tool routes, requires authoritative
post-action inspection, and requires resolving named registered repositories before asking for a
path. `inspect_state` now exposes bounded repository identity and canonical paths. The compact
goal rendering fix separately restores space for a visible outcome.

Regression coverage:
Prompt tests cover the complete-route and repository-resolution contracts; Manager MCP tests
cover repository visibility and bounds.

Replay result:
Passed with Sonnet after repeated Haiku replays continued to ignore the explicit tool contract.
Sonnet resolved `tiny-app` from authoritative state, created the job, and launched a native Haiku
worker without an internal ID or path supplied by the user. The residual Haiku behavior is
classified as model capability, not a remaining Switchboard correctness defect.

### Controller restart preserves runtimes but leaves stale native attention

Severity: P2

Category: recovery / native integration / observability

Scenario:
Restart the Switchboard controller while a Manager is ready and its workflow worker is believed to
be waiting for native permission.

Steps to reproduce:
1. Start a Manager-coordinated workflow until the board reports `permission required` for a worker.
2. Quit only the board controller with `Ctrl+Q`; leave both tmux/Claude processes alive.
3. Restart `sb` against the same isolated state.
4. Select the attention worker with `Ctrl+J` and enter it with `Ctrl+E`.

Expected:
Both runtimes are adopted exactly once, and recovered attention reflects what the exact native
process currently requires.

Actual:
The Manager and worker are adopted exactly once with Manager ownership preserved. The board still
reports `permission required`, but the exact Haiku process is idle at an empty Claude prompt in plan
mode; there is no permission dialog to answer. After detach and confirmed handback, the stale item
clears.

Evidence:
The recovered board showed two sessions and one attention item. The exact worker screen showed
Claude Code 2.1.220, Haiku 4.5, the same disposable repository and an empty `❯` composer. No runtime
was duplicated.

Reproducibility: Once; covered deterministically by the recovery test named below.

Workaround:
Enter the worker, verify its composer is empty, detach, and confirm handback.

Diagnosis:
Recovery mapped a ready runtime to an idle worker but resolved no attention, leaving a delivered
native permission notification actionable after the native prompt had disappeared.

Resolution:
On recovery, a ready runtime now resolves only obsolete native `permission required` attention;
workflow decisions and plan approvals remain actionable.

Regression coverage:
`test_recovery_adopts_an_exact_live_runtime` seeds both permission and plan-approval items, adopts
the ready runtime, and asserts only plan approval survives. It passes at `2e3fcd9`.

Replay result:
The first public replay exposed and rejected an over-broad implementation that cleared approval
too. The narrowed fix passes deterministic and real-tmux replay. The live approval-boundary replay
is the same one still owed by the finding above; see the reconciliation section.

### Incomplete workflow looks complete after controller recovery

Severity: P1

Category: workflow / recovery / UX / observability

Scenario:
Continue a tiny `lightweight-feature` job after its planner worker survives a controller restart.

Steps to reproduce:
1. Ask Manager to implement the tiny greeting change with `lightweight-feature`.
2. Let Manager create the job and native planner, then restart only the controller while the
   worker survives.
3. Return ownership after entering the recovered idle worker.
4. Ask Manager naturally: `what's still running?`
5. Ask: `Is the greeting task complete? If not, why did it stop?`

Expected:
The workflow resumes or clearly reports its incomplete/blocked stage and the action needed. Status
questions distinguish idle workers from completed jobs.

Actual:
The board shows an idle planner whose detail says `workflow plan-feature`; the requested code and
test are unchanged. Manager answers `Nothing needs you. 0 worker(s) still working.` to both status
questions. The board has no completion evidence and gives no reason the composite stopped.

Evidence:
The exact worker is idle at an empty native prompt. The public workflow catalog describes
`lightweight-feature` as implement plus smoke-test and finalize, while the surviving worker is only
the `plan-feature` stage. The disposable repository remains unchanged.

Reproducibility: Always for both natural-language status questions in this recovered state.

Workaround:
None discoverable from the board without implementation knowledge; manually inspect the target
repository and restart the task.

Diagnosis:
`status_summary` counted only working workers and attention, not nonterminal jobs. Separately, the
Manager contract's unconditional `A new feature ticket starts with plan-feature` overrode an
explicitly named composite and encouraged Sonnet to create an orphan atomic planner.

Resolution:
Status now names idle incomplete jobs instead of declaring there is no work. The Manager contract
now requires an explicitly named composite to be started as a composite rather than replaced by its
atomic planning stage.

Regression coverage:
Prompt coverage asserts explicit composite routing; status coverage asserts an idle incomplete job
is named with its stage.

Replay result:
Partially passed. A fresh Sonnet Manager started the named `lightweight-feature` run, Haiku produced
contracts, and the run stopped conservatively after exact-process human intervention. The board
now says the job is incomplete and idle. Natural interrupted-run replay is tracked separately.

### Manager header says ready while the current turn still rejects follow-ups

Severity: P2

Category: UX / native integration / observability

Scenario:
Ask why a workflow is blocked and immediately approve its plan when the exact native Manager has
already returned to an empty prompt.

Steps to reproduce:
1. Reach the approval boundary after `plan-feature` in a native composite run.
2. Submit `Why is the greeting task blocked?`
3. Wait until the board header says `Manager · ready · manager` and the composer is available.
4. Submit `I approve the greeting task plan. Continue the lightweight-feature workflow.`

Expected:
`ready` means the follow-up will be handled, or the UI visibly disables submission and says the
previous Manager turn is still finishing.

Actual:
The approval text disappears from the composer but never becomes Current goal and no workflow
action occurs. Current goal remains the blocker question. The header says ready throughout because
the native process is at an empty prompt even though the board's asynchronous Manager handler is
still busy.

Evidence:
The 80x24 capture shows `Manager · ready · manager`, then the full approval in the composer, then an
empty composer with Current goal still `Why is the greeting task blocked?`. The run remains at the
plan approval boundary.

Reproducibility: Twice around slow native Manager turns.

Workaround:
Wait substantially longer than the visible ready transition, then retype the follow-up.

Diagnosis:
The title used only native runtime state, which can become ready before the board coroutine has
finished applying the turn. Manager log entries were painted oldest-first into a clipped pane, so
the newest result could sit below old recovery notes.

Resolution:
While the board coroutine is active, the title now says `turn active` regardless of native process
state. Manager log entries paint newest-first so the latest outcome owns the visible rows.

Regression coverage:
UI tests cover a native-ready/board-busy title and latest-outcome visibility after an older note at
80x24.

Replay result:
Latest objective/status visibility passed after controller restart.
`test_manager_title_does_not_claim_ready_while_board_turn_is_busy` covers the native-ready/board-busy
title deterministically and passes at `2e3fcd9`. The clean *live* rapid-follow-up replay was blocked
in the first session by an exhausted approval quota and was not re-attempted in the second, which
had no slow Manager turn to race; it is still owed. See the reconciliation section.

### Manager does not resume a naturally referenced interrupted composite

Severity: P2

Category: workflow / UX / native integration

Scenario:
Resume a composite run that exact-process entry correctly paused as human-intervened.

Steps to reproduce:
1. Enter a planner during its managed turn, detach with an empty composer, and return control.
2. Observe the durable run blocked with `The user attached to this worker.`
3. Tell Manager `Resume the greeting run. I approve replaying the interrupted planning stage.`
4. Tell Manager `Continue what I interrupted.`

Expected:
Manager resolves the sole interrupted run and resumes it, causing the tainted stage to replay.

Actual:
Both messages become Current goal, but Sonnet performs no orchestration action. The run remains
blocked, human-intervened, at iteration `{"0": 1}`.

Evidence:
The board remains idle after both turns. Durable inspection after the black-box repro confirms
`lightweight-feature | blocked | The user attached to this worker` with no iteration change.

Reproducibility: Always in two Sonnet turns.

Workaround:
None through ordinary natural language; an implementation-aware caller could invoke `resume_run`.

Diagnosis:
The Manager contract says follow-ups go to an existing worker but does not distinguish a paused
composite run, whose conservative human-intervention replay is controlled by `resume_run`.

Resolution:
The Manager contract now distinguishes paused composites from ordinary worker follow-ups and
requires natural continue/resume requests to resolve the run and call `resume_run`.

Regression coverage:
Prompt tests assert both the natural `continue what I interrupted` phrase and the `resume_run`
route.

Replay result:
Passed. A fresh Sonnet Manager carrying the revised contract answered `Continue what I
interrupted.` by resolving the sole paused run and calling `resume_run`; the tainted
`implement-approved-plan` step replayed as a new implementer session. Commit `e1e3276`.

## Second dogfood session

The findings above were recorded before this session. Everything from here was found by using
Switchboard as a user, from a clean isolated state, with real native Claude under real tmux.

Scope was deliberately narrowed for this session: realistic daily use over exhaustive robustness
testing. Chaos matrices, timing permutations, and pathological terminal cases were not pursued;
items judged low-value under that rule are listed as deferred at the end.

### Setup

- Runtime: this checkout via `uv tool install --editable`, driven through a real PTY so tmux entry
  behaves exactly as it would for a user. `TMUX` is unset in that PTY, so `Ctrl+E` attaches as a
  normal external client.
- State: `SB_HOME=/tmp/sbdog/sbhome`, `SB_CONFIG=/tmp/sbdog/config.yaml`. The real `~/.config`
  and `~/.switchboard` were never touched.
- Models: Sonnet for Manager, planner, implementer, reviewer; Haiku for verifier and general.
- Targets: a disposable `tiny-app` repository and a disposable clone of Switchboard itself. The
  working checkout was never a managed target.
- Native Claude: Claude Code 2.1.220, tmux 3.7b.

### An adopted Manager silently loses every orchestration tool

Severity: P1

Category: recovery / native integration / correctness

Scenario:
Quit the board with a healthy native Manager alive, restart, and give it a goal.

Steps to reproduce:
1. `sb --register <repo>`; let Manager reach `ready`.
2. Quit the controller. The native Manager and its tmux server survive by design.
3. Restart `sb` against the same state. The board reports the Manager adopted and `ready`.
4. Submit any goal, for example `In tiny-app, make greet() return "Hello, there!" when the name
   is empty. Use the lightweight-feature workflow.`

Expected:
The Manager routes the request, or refuses with a reason.

Actual:
The board shows `Manager · ready · manager`, the turn completes, and nothing happens: no job, no
worker, `Nothing needs you`. The exact Manager transcript contains `[Tool: list_repositories]` and
`[Tool: list_workflows]` printed as prose. `/mcp` inside the session reports
`switchboard · ✘ failed`.

Evidence:
`ps` showed the `switchboard.agents.manager_mcp` bridge process gone while the Manager's Claude
process was still alive; the board had already rebound its own socket at the same path.

Reproducibility: Always.

Diagnosis:
`_proxy` opened one connection to the board's Unix socket and raised
`RuntimeError("Switchboard manager service disconnected.")` on EOF. Quitting the board therefore
killed the bridge, and Claude Code never respawns a stdio MCP server that exits. The Manager keeps
running with no `mcp__switchboard__*` tools at all and invents plausible tool markup instead. This
also explains the earlier finding above where a Manager narrated a workflow it never started: the
prompt was tightened repeatedly for what was really a dead tool surface.

Resolution:
The bridge reconnects to the same generation-bound socket path, which a fresh controller rebinds on
recovery. A request already delivered is never resent, so a mutation cannot be applied twice, and an
unreachable board becomes a JSON-RPC error the Manager can report. Commit `72ad7b8`.

Regression coverage:
A board stub is bound in a subprocess, the bridge is exercised, the stub is killed (a real process
exit, which is how a controller actually goes away), a new stub rebinds the same path, and the same
bridge must still answer. Verified to fail without the fix (`BrokenPipeError`, bridge dead).

Replay result:
Passed. The next Manager generation called real tools -- `Called switchboard 2 times` -- resolved
`tiny-app` from authoritative state, created the job, and started the composite run.

### The one remedy the product offers for a trust prompt is refused

Severity: P1

Category: native integration / correctness / UX

Scenario:
Start a workflow in a repository Claude has not seen before.

Steps to reproduce:
1. From a clean state, ask Manager to run `lightweight-feature` on a fresh repository.
2. The planner stops at Claude's workspace-trust prompt. The board raises attention reading
   `Native Claude startup needs human attention. Press Ctrl+E to enter this session and handle
   workspace trust, login, or another startup prompt.`
3. Select that worker and press `Ctrl+E`, exactly as instructed.

Expected:
The exact native session opens so the trust prompt can be answered.

Actual:
`Cannot enter that session: Runtime has no durable tmux target identity.` The tmux session plainly
exists and is holding the trust prompt. The run has no way forward.

Evidence:
`tmux -S <sock> ls` listed the worker session; the durable runtime row had `substrate {}`.

Reproducibility: Always on a first-use repository.

Diagnosis:
`_start_backend` reads the runtime row before launching. The supervisor persists the tmux substrate
identity during launch, then waits for `SessionStart`. When that wait times out at the trust prompt,
the failure path wrote back the pre-launch snapshot and erased the substrate.

Resolution:
Refresh the runtime row before recording the blocked state. Commit `2a9ee9b`.

Regression coverage:
The existing startup-timeout test now persists a substrate before raising, exactly as the real
supervisor does, and asserts it survives. Verified to fail without the fix.

Replay result:
Passed from clean state: `Ctrl+E` opened the exact session, the trust prompt was answered, handback
returned ownership, and the board reported `Native startup completed; the pending workflow prompt
was delivered.`

### Clearing the trust prompt leaves the run permanently blind

Severity: P1

Category: workflow / recovery / observability

Scenario:
Continue after resolving a native startup prompt, following the product's own instructions.

Steps to reproduce:
1. Reach the trust prompt above and clear it via `Ctrl+E`.
2. Detach and confirm handback.
3. Watch the board.

Expected:
The planner runs and the run advances.

Actual:
The planner really does run and completes its turn -- the contract JSON is visible in its pane --
but Switchboard observes none of it. The board says `1 worker(s) still working` indefinitely, no
artifact is harvested, and the run stays `blocked` on the original startup error. Asking Manager to
resume produces only a duplicate-worker refusal.

Evidence:
Durable state showed the run with `current_worker_id: null` and detail
`Could not start worker ...: Timed out waiting for native Claude SessionStart.`, while the runtime
was `turn_complete` and the delivered Stop hook had produced no `worker.completed` event.

Reproducibility: Always, following the previous finding.

Diagnosis:
Two causes. `_start_backend` creates the event pump only on its success path, so a worker that
blocked at startup has a live backend session that nobody consumes. And `_advance_run` records
`current_worker_id` only when `start_workflow` returns, so the created-but-blocked worker was never
linked to its step -- which is why answering it could not unblock the run and why a resume met the
duplicate refusal.

Resolution:
Own the created worker on the blocked step, and keep one pump per live backend session however it
was started. Commit `db56726`.

Regression coverage:
A composite run whose worker blocks at startup must link that worker and have a pump; clearing the
prompt must put the run back to `running` and the worker to `working`. Verified to fail without the
fix.

Replay result:
Passed from clean state, and then all the way through: plan produced and harvested, approval gate
reached, implementation committed in an isolated worktree, verification passed.

### A run waiting on the user says nothing needs the user

Severity: P1

Category: workflow / UX / observability

Scenario:
Let a `lightweight-feature` run reach its plan-approval gate.

Steps to reproduce:
1. Ask Manager to run `lightweight-feature` on a small change.
2. Let the planner finish normally, without asking a question.
3. Read the board.

Expected:
The board says an approval is pending.

Actual:
`Nothing needs you right now, but 1 incomplete job(s) are idle: ... (planning).` No attention item,
nothing in the detail pane about the gate. The run is durably `awaiting_approval` and the only way
to discover that is to ask the Manager, which answers correctly: "It's waiting on your approval of
the implementation plan."

Evidence:
Durable run status `awaiting_approval`, detail `implement-approved-plan needs an approved
implementation contract`, and zero attention items.

Reproducibility: Always when the planner ends its turn cleanly.

Diagnosis:
Plan-approval attention was raised only on the path where a planner *blocks with a question*. A
planner that ends cleanly resolves its own attention, and the approval gate the run then stopped at
raised none of its own.

Resolution:
Raise plan-approval attention wherever a run pauses for the user. Commit `c6366cf`.

Regression coverage:
A run reaching the gate must produce exactly one `plan_approval` item and a status summary naming
it; approving must retire that item. Also covers a related hole found while fixing it -- entering a
worker resolves its attention so auto-advance does not bounce back, which silently retired the gate;
the gate is durable run state, so it now returns on handback.

Replay result:
Passed live: `1 worker(s) need attention: 1 plan approval.`

### Saying you approve the plan is not accepted as approving the plan

Severity: P1

Category: UX / correctness

Scenario:
Approve a plan in ordinary English.

Steps to reproduce:
1. Reach the plan-approval gate.
2. Submit `Yes, I approve the plan. Continue the run.`

Expected:
The plan is approved and the run continues.

Actual:
The Manager reports the refusal honestly and is stuck:
"The `approve_plan` call is being refused (`"Plan approval must be explicit in the current user
turn"`) despite your explicit approval -- this looks like a system-side issue... Can you confirm
again, in this exact message, that you approve". The user is asked to re-approve what they just
approved, with no hint of what phrasing would work.

Reproducibility: Always.

Diagnosis:
`APPROVE_RE` was anchored `^...$`, so the entire message had to be the approval phrase. The safety
property that matters is *whose turn the approval appears in* -- not that the message contains
nothing else.

Resolution:
Recognise an approval stated in any sentence of the user's own message, still rejecting questions,
conditions, and negations. Destructive-operation confirmation stays deliberately narrow, because
the friction there is worth keeping. Commit `29a23bd`.

Regression coverage:
Nine phrasings that must grant approval and eighteen that must not, the withheld set grouped by why
each is withheld — question, negated, withdrawn, conditional, future, quoted, third party,
instruction, refusal — after the independent review showed the first version of this test was
tautological.

Replay result:
Passed live: `Yes, I approve the inventory plan. Please continue that run...` was accepted and the
implementer started.

### Answering a worker's permission prompt leaves it blocked forever

Severity: P1

Category: native integration / workflow / recovery

Scenario:
Enter a worker to grant a tool permission, then leave.

Steps to reproduce:
1. Let an implementer reach `Permission required for Edit.`
2. `Ctrl+E`, answer the prompt, let the turn finish, detach, confirm handback.
3. Ask Manager to continue the run.

Expected:
The worker is no longer blocked and the run continues.

Actual:
The worker stays `blocked` with the stale reason. Because `BLOCKED` is non-terminal, replaying that
step is refused as a duplicate workflow, so the run cannot move at all. The Manager's advice --
"attach to that worker session directly to grant the permission" -- points at a permission that was
already granted.

Evidence:
Verifier `fb3848fb` durably `blocked` on `Permission required for tool.` with its runtime `ready`
and its verification report complete in the pane; run `blocked` with
`already has smoke-test on ...`.

Reproducibility: Always.

Diagnosis:
Recovery reconciles worker status against the observed runtime; handback did not.

Resolution:
Reconcile on handback the same way recovery does. Commit `3e6eb6e`.

Regression coverage:
A worker whose runtime is no longer waiting returns to idle on handback and its obsolete permission
attention is resolved; a worker whose runtime is genuinely still waiting keeps its block.

### One early retry stalls a job permanently

Severity: P1

Category: native integration / recovery / correctness

Scenario:
Ask Manager to resume two runs while one worker is still starting.

Steps to reproduce:
1. Have a worker whose native session is still starting.
2. Ask Manager to resume its run, so a managed send races startup.
3. Wait for the session to become healthy, then retry.

Expected:
The first attempt is refused as premature; the retry works.

Actual:
The job never recovers. The board shows the worker `idle` while every send is refused with
`Native Claude runtime is waiting, not ready`, and the Manager can only say "its native runtime
keeps reporting not ready even though the worker itself shows idle". A sibling worker was marked
`disconnected` -- a terminal status -- with `Could not deliver input: Native Claude runtime is
starting, not ready.` while its session was perfectly healthy.

Evidence:
Two workers in this state simultaneously; the "idle" one was sitting at Claude's own interactive
question dialog, fully alive.

Reproducibility: Always when input races startup.

Diagnosis:
`SessionManager.send` treated *any* send failure as a failed delivery: it marked the worker
`DISCONNECTED` and forced the runtime to `WAITING`. But the refusal is a precondition check that
delivered nothing -- and writing `WAITING` made it self-fulfilling, since the runtime then failed
its own readiness check on every later send.

Resolution:
An explicit `WorkerNotReadyError` on the backend protocol for a precondition refusal that delivered
nothing; durable state is left untouched for it. A genuine delivery failure still fails closed.
Commit `38fd5f7`.

Regression coverage:
A send that races startup must not change worker status or runtime state and must raise a
retryable error; the existing test that a real delivery failure marks the worker disconnected still
passes unchanged.

### Answering the agent's own question throws away its work

Severity: P2 (design tension, not fixed)

Category: workflow / product design

Scenario:
A planner uses Claude Code's interactive question UI to ask a genuine design question.

Steps to reproduce:
1. Give a worker a task with a real ambiguity.
2. It renders Claude's own question dialog and waits.
3. Enter the session, choose an option, let it finish, hand back.
4. Ask Manager about the job.

Expected:
The answer unblocks the worker and its output counts.

Actual:
"no plan has been produced yet for sb-clone, so there's nothing for me to approve". The planner
worked for roughly twenty minutes, produced a full plan and behaviour contract, and all of it was
discarded, because entering the session tainted the attempt.

Diagnosis:
Switchboard's human-intervention taint model assumes entering a session means the user edited things
by hand, so the attempt cannot be trusted to advance the run. In practice the most common reason to
enter is to answer a prompt the agent itself raised -- a permission dialog or a question. Those are
answers *to* the agent, not edits around it, and discarding the resulting work is expensive and
surprising.

Resolution:
None. This is a product decision about what "human intervention" means, not a bug to patch under
dogfooding. Recorded as the most significant open design question from this session. A plausible
direction: distinguish "the user answered a prompt the session raised" from "the user typed into
the session", since the former is already observable from the hook stream.

### Documented `Ctrl+Space` does not jump to attention in a real terminal

Severity: P3 (deferred)

Category: UX

`Ctrl+Space` (`Jump to the next attention item`) had no effect in the live PTY board, with
auto-advance both on and off, while `Ctrl+J` worked in the same session. Driven headlessly through
Textual's pilot, both `ctrl+space` and `ctrl+at` select the attention worker correctly, so the
application logic is right and the gap is in terminal key decoding.

Deferred under this session's scope rule: a convenience key with two working alternatives
(`Ctrl+J`, and auto-advance which already routes attention automatically). Worth recording for its
own sake that the existing headless coverage does not prove the terminal behaviour.

## Scenario log

| Scenario | Result |
| --- | --- |
| First launch, fresh `SB_HOME`, Manager workspace trust via `Ctrl+E` | Works; guidance names `Ctrl+E` correctly |
| Quit controller, restart, adopt live Manager | Adopted exactly once; exposed the dead MCP bridge (P1, fixed) |
| Natural-language goal to a composite workflow | Manager resolves the repo by name, creates the job, starts the run |
| Repository first-use trust prompt on a worker | Exposed two P1s (refused entry, blind run); both fixed and replayed |
| Plan produced and harvested | Implementation and behaviour contracts both stored, shown as `evidence ✓ ✓` |
| Plan-approval gate | Exposed silent gate (P1) and rejected approval phrasing (P1); both fixed and replayed |
| Implementation in an isolated worktree | Real edit, real test, real commit `90c921d` on `sb/...` branch |
| Verification step | Read-only verifier ran in the authoritative worktree; AC1/AC2/AC3 passed |
| Human entry during an active turn, then `Continue what I interrupted.` | Run resumed via `resume_run`, tainted step replayed |
| Two concurrent jobs across two repositories | Both tracked; board names both incomplete jobs and the exact worker needing attention |
| Managed send racing startup | Exposed permanent stall (P1); fixed |
| Native permission prompts during work | Surfaced accurately as attention with the tool name |
| Worker asks a design question via Claude's question UI | Answered, but the attempt is tainted and its work discarded (open design question) |
| Worktree placement, branch naming, repo isolation | Correct; see below |
| Process cleanup after quitting the board | Runtimes survive by design, but nothing can reclaim them; see below |
| Narrow terminal | Not re-tested; covered by the earlier 80x24 finding and its regression test |

## Worktree and cleanup behaviour

Verified directly after the session:

- Writable workers get exactly one worktree, only under the managed root:
  `/tmp/sbdog/sbhome/worktrees/tiny-app/add-inventory.remove-name-count--implementer-7a1ed79c`,
  branch `sb/add-inventory.remove-name-count--7a1ed79c`.
- Read-only planners and verifiers get no worktree and observe the job's authoritative path.
- Nothing was created inside either user repository, and both stayed on `main`.
- No branch was deleted and nothing was pushed, force-pushed, or merged.

Process cleanup is the weak spot, and it is a real finding rather than a bug:

Quitting the board deliberately leaves native runtimes alive so the next controller can adopt them.
That is the right default. But nothing ever reclaims them, and `sb` exposes no way even to see
them: the command surface is `claude`, `workflows`, `config`. After this dogfooding machine had run
several throwaway `SB_HOME`s, **14 orphaned tmux servers and 24 orphaned native Claude processes**
were alive with no product-level way to list or stop them. This is the finding chosen for the
self-hosting experiment below.

## Self-hosting: implementing a Switchboard finding through Switchboard

The task delegated to Switchboard, in a disposable clone of this repository, in plain English:

> In sb-clone, add a read-only `sb runtimes` subcommand that lists the native runtimes Switchboard
> has recorded (agent kind, generation, process state, owner, and tmux session name) so a user can
> see what was left running. Use the lightweight-feature workflow.

What happened:

- The Manager resolved `sb-clone` from registered state, created the job, and started the composite
  without being given an ID or a path.
- The planner hit the first-use trust prompt; the board named the exact worker and the exact key.
- The planner used a subagent (`Explore RuntimeInstance model and storage`) entirely on its own --
  Claude's capability, inside a Switchboard-managed session, with Switchboard none the wiser and
  none the worse.
- It then asked a genuinely good design question through Claude's question UI: with only the five
  requested fields, two runtime rows can be indistinguishable, so should the listing include a short
  agent id? It recommended yes. That is a better specification than the one it was given.
- Answering that question required entering the session, which tainted the attempt. The plan and
  contract it produced were therefore discarded, and the Manager correctly reported that no plan
  existed.

Honest outcome: **the self-hosting task produced a real plan and a real design improvement, but did
not reach a committed implementation within this session**, and the reason it did not is the taint
interaction recorded as an open design question above. That is a finding about Switchboard, not a
failure to try, and it is exactly what the experiment was for. It was not worked around by
implementing the feature by hand.

By contrast, the same composite in `tiny-app` did run end to end through Switchboard: a real edit,
a real test, a real commit in an isolated worktree, and a passing verification report -- with the
user never typing code.

## Does Switchboard earn its place?

Where it adds value beyond raw Claude Code:

- **The job/worker/worktree graph is real.** Three sessions across two repositories, each in the
  right directory, one writable worktree per job, read-only roles observing the authoritative
  lineage. Doing this by hand with `claude` in several terminals is exactly the bookkeeping people
  get wrong.
- **Durable state survives the controller.** Quitting and restarting `sb` adopted live sessions
  exactly once, with no duplicates and no lost identity. This is the single most convincing part of
  the design.
- **Artifacts as first-class evidence.** `evidence implementation_contract ✓, behavior_contract ✓`
  in the detail pane, harvested from a fenced JSON block, is genuinely better than scrolling a
  transcript.
- **Approval gates are enforced in Python, not by asking a model nicely.** The implementation step
  provably cannot start without an approved contract.
- **The Manager as a status oracle is good.** Every time the board was ambiguous, asking the
  Manager in English produced an accurate, specific answer -- including admitting a refusal it did
  not understand.

Where it currently gets in the way:

- **Native prompts dominate the loop.** Every new repository and every new worktree costs a trust
  prompt, and the first Edit and Bash of every writable worker cost permission prompts. In this
  session, the human interventions were almost entirely "answer a Claude Code dialog", not
  "supervise the work". The supervise-without-entering-terminals promise is undermined by the
  entering being mandatory.
- **And entering is expensive**, because it taints the attempt. The two costs compound.
- **Attention navigation is thin.** Reaching the session that needs you means cycling `Ctrl+J` and
  reading the detail pane; landing on the wrong one and pressing `Ctrl+E` silently taints an
  actively working session with no confirmation.
- **Failure text is better than failure recovery.** Several messages were precise and actionable
  and still described a dead end (`Press Ctrl+E` when entry was refused; "attach to grant the
  permission" when it was already granted). Every P1 this session was a state-reconciliation gap
  behind a well-written sentence.

Where Claude Code already does the job and Switchboard should stay out:

- Subagents, plan mode, the agent loop, session persistence, settings inheritance, and the
  permission UI all worked untouched inside managed sessions. The planner's own use of a subagent
  and of the question UI needed nothing from Switchboard. The right posture, as `CLAUDE.md` says,
  is to keep deleting anything Claude already does well -- and the interactive question UI is a
  case where Switchboard should *integrate with* rather than route around Claude's behaviour.

## The development workflow itself

This project was built with an unusual division of labour: ChatGPT as an independent planner and
reviewer, Codex executing plans, Switchboard coordinating agents, and finally Switchboard used to
dogfood and fix Switchboard.

Observations from this session, which is the first where the last step was actually exercised:

- **Separating planning from execution worked, and the repository shows why.** The phase documents
  and `CLAUDE.md` carry the durable decisions; the commits carry mechanical changes against them.
  An implementation agent starting cold could reconstruct intent without the chat history. This is
  the same benefit as a written design doc, obtained under conditions where context is otherwise
  routinely lost.
- **Independent review caught what the implementer could not.** The single most valuable pattern
  here is a reviewer that did not write the code and does not share its assumptions. Two of this
  session's fixes were narrowed after review-style scrutiny found them over-broad.
- **Executable validation is what keeps agents honest.** Every fix in this session was required to
  fail without the change and pass with it, verified by actually reverting the source and rerunning.
  That step caught one fix whose test would have passed either way, and one placement that was pure
  speculation and was deleted rather than committed. Tests written *after* a real reproduction are
  worth far more than tests written from a description.
- **Dogfooding found what testing did not.** The suite was green at 362 tests before this session
  and every P1 above was live only. The MCP bridge, the erased tmux target, the missing pump, the
  silent approval gate, and the self-fulfilling not-ready refusal were all invisible to unit and
  integration tests because each depended on a real subprocess lifecycle crossing a real controller
  restart. The tests were not bad; they were testing the pieces, and the bugs lived between them.
- **The reproduce-first discipline mattered more than any tool.** Twice, inspecting internals first
  would have produced the wrong fix: a stray `start it` in a composer turned out to be Claude Code's
  own ghost-text suggestion, and a "broken" `Ctrl+Space` turned out to be a missing key mapping in
  the test harness. Both were dismissed by evidence rather than patched.
- **The honest limit:** an agent dogfooding its own project is not an independent user. It knows
  where to look, tolerates friction a real user would not, and is tempted to explain away rough
  edges. The friction findings above are the ones most likely to be understated.

## Independent review

A fresh agent reviewed `5560f9b..HEAD` with no knowledge of how the fixes were reached, briefed
only to hunt regressions, races, safety-invariant weakening, subprocess edge cases, and tests that
do not prove what they claim. It ran the suite, ruff and mypy itself, and wrote standalone probes
against the real `SessionManager` and `_Bridge`.

Verdict: no blocking findings. Four important, five minor, and a specific critique of five tests.
All four important findings were in code this session had just added, and all were valid:

1. **`detach` cleared a block the user never answered.** The condition was "runtime is not
   WAITING", but `STARTING`, `EXITED` and `ABSENT` all satisfy that. A user who pressed `Ctrl+E`,
   looked at the trust prompt, decided not to deal with it and left had the block *and* its
   instructions silently wiped — reintroducing the invisible-stall class the rest of the stack
   fixes, and letting the step be replayed while the first session still held its worktree. Now
   only `READY` reconciles.
2. **The widened approval matcher failed in both directions.** It granted on
   `"The plan looks good. Do not implement until I have spoken to Sam."` (sentence-scoped negation
   meant punctuation decided whether an explicit refusal counted), on relayed third-party approval
   such as `"Worker output: lgtm from the reviewer."`, and on `"Looks good, but wait for CI."`; it
   withheld on `"Go ahead, no rush."` and `"Approve it when you can."` because `no` and `when` were
   in the stop-list. The guard is the only deterministic half of the approval invariant, so this
   mattered. Rewritten: the approval must open its own sentence in the user's voice, quoted text is
   somebody else relaying an approval, and any withholding instruction vetoes the whole message
   however it is punctuated.
3. **An approval gate could point at an unrelated worker.** With no current worker, the fallback was
   the newest non-terminal worker of the job. The reviewer's probe raised the gate on a reviewer the
   user had started themselves, so pressing Enter on the attention item opened a session that never
   wrote the plan. Now it matches a worker belonging to the run's own workflow, or raises nothing.
4. **The MCP bridge misreported an applied-then-vanished call.** The retry logic was confirmed
   exactly-once in all three realistic cases, but both failure branches returned "not reachable…
   retry once the board is running" — including the branch whose own comment says the call may
   already have been applied, inviting the Manager to duplicate a mutation. The two branches now say
   different things.

Minor findings accepted and fixed: `WorkerNotReadyError` was classified from "not READY", which
swallowed genuinely dead runtimes (now `STARTING`/`WAITING` only); `_ensure_pump` could keep a pump
bound to a superseded backend session after a re-launch; and a gate satisfied by resuming rather
than approving stayed open forever — the exact inverse of the bug that raised it.

Not fixed, recorded instead: the manager socket path is visible in `ps` and the reconnect loop
widens a pre-existing local-squat window on a multi-user host from a one-shot startup race to a
recurring 30-second one. Out of scope for a single-user tool; noted as a limitation.

The test critique was the most useful part. The reviewer showed that
`test_ordinary_human_sign_off_counts_as_approval`'s withheld half was tautological — every case
contained a literal stop-word, so it restated the keyword list rather than probing it, and none of
the real failures were covered. Also that the not-ready test monkeypatched the error rather than
exercising the code that decides to raise it, that the handback test covered only the two states
that worked, and that the bridge's interesting branches were untested. All four gaps are now closed,
and the withheld cases are grouped by *why* each is withheld.

Verifying the fixes exposed one more thing worth recording: the first version of the gate-worker
regression test passed against the buggy code, because the run still had a `current_worker_id` and
never reached the fallback. It only became evidence once the test set that field to `None`, which is
the state the real path is reached in.

## Reconciliation of first-session items at closeout

Three first-session findings were still carrying `pending` replay markers when the report was
otherwise complete. Their current status, established at `2e3fcd9` rather than from memory:

- **Recovery must preserve a plan approval** (`5560f9b`). Deterministic
  coverage is `test_recovery_adopts_an_exact_live_runtime`, which passes. The second session restarted
  the controller onto a live Manager repeatedly — that is how the dead MCP bridge was found — but
  never with an approval outstanding, so the live approval-boundary replay was **not** performed. The
  claim is deterministic, not live.
- **`turn active` while the native process is already idle** (`d5b8f6e`). Deterministic coverage is
  `test_manager_title_does_not_claim_ready_while_board_turn_is_busy`, which passes. The live
  rapid-follow-up race was never replayed: blocked by quota in the first session, and the second
  session produced no slow Manager turn to race against. Still owed.
- **The composer-retention finding** was rejected as a harness artefact, so it has no coverage to
  owe; its `Pending` markers were bookkeeping noise and are now marked not applicable.

One further bookkeeping error was found and corrected while reconciling: the configuration-mismatch
finding had the recovery/attention resolution of a different finding pasted into it, which would
have read as a fix it never received. Its diagnosis, resolution and coverage now describe the
visibility fix that actually applies to it.

Both outstanding items are live replays of fixes that already have passing deterministic coverage.
Neither blocks Phase 8; both are carried forward as owed evidence rather than quietly closed.

## Final validation

Run at `2e3fcd9`, the final code commit, and re-run at closeout with only this document modified.

- Full suite including real-tmux integration tests: **373 passed**
  (`./.venv/bin/python -m pytest -q`). The tmux-backed tests are not gated by any environment
  variable; they ran as part of this.
- `./.venv/bin/ruff check src tests` — All checks passed.
- `./.venv/bin/mypy` — Success, no issues in 46 source files.
- `git diff --check` — clean.
- Every fix, including all of the review fixes, verified to fail without its change by reverting
  only that change and rerunning the specific test.
- Live native/tmux replays of the resume, trust-prompt entry, blind-run, approval-gate, and
  approval-phrasing fixes, each from a clean isolated state.
- Worktree, branch, and repository isolation verified directly on disk after the session.
- The working tree is clean and this checkout has exactly one worktree, still on `main`.

Process cleanup, measured at closeout rather than asserted. The second session's own runtimes under
`/tmp/sbdog` are stopped: nothing holds that socket. But **11 tmux servers and 21 native Claude
sessions from earlier Phase 8 runs are still alive**, across `/private/tmp/switchboard-native-phase3*`,
five ad-hoc `/private/tmp/switchboard-tmux-*.sock` servers, three `/tmp/switchboard-phase8.zxh2GV`
states, and the default `~/.local/share/switchboard` state. That default state holds one registered
repository (this checkout), one job, and one idle **read-only** planner with no worktree, so nothing
wrote to the checkout — but its Manager runtime is orphaned exactly like the rest.

This is the reclamation finding reproducing itself during its own closeout, which is the strongest
evidence in this report that it is real: an agent that had just written the finding down, and had
`sb` in front of it, still had no product-level way to see or stop these. They are left running
deliberately rather than killed by hand, so the next phase starts from the honest state.

## Known remaining limitations

- Answering a prompt the agent itself raised taints the attempt and discards its work (open design
  question above). This is the most consequential item left.
- `Ctrl+Space` does not reach the application in a real terminal; deferred, with two workarounds.
- `sb` still has no way to list or reclaim orphaned runtimes. The self-hosting experiment planned
  this feature but did not land it; the finding stands, and 11 tmux servers with 21 native Claude
  sessions were still alive at closeout.
- Two live replays are owed rather than closed (recovery across a pending plan approval, and the
  rapid Manager follow-up race). Both fixes have passing deterministic coverage; see the
  reconciliation section.
- The status summary describes an approval-gated run as an "idle incomplete job". The attention
  queue is now correct, but that sentence is still imprecise.
- A single Manager message longer than the (fixed-height, scrollback-free) Manager pane is still
  clipped at the bottom. Newest-first painting guarantees its beginning is visible, not its end.
- Read-only workers keep Bash, so read-only remains a tool-policy and prompt guarantee rather than
  a sandbox (pre-existing; see `troubleshooting.md`).
- The manager MCP socket path is visible in `ps`, and the bridge's reconnect loop widens the
  pre-existing local socket-squat window on a multi-user host into a recurring 30-second one.
  Accepted for a single-user tool.
- The approval matcher is a heuristic over English. It is now tested against twenty-seven phrasings,
  eighteen of them adversarial, but it stays a lexical guard: the durable property it enforces is that the approval
  appeared in the user's own current turn, not that a model understood it.
- Deferred as low-value under this session's scope: chaos and crash matrices, slow-hook timing
  permutations, combinatorial dependency graphs, and pathological terminal sizes.

## Environment notes

Nothing in this session was blocked by the environment. Real tmux, real native Claude Code 2.1.220,
real subprocess entry and handback, and atomic commits were all available. The first session
attributed several `Pending` replays to an exhausted escalation quota; the resume, trust-prompt
entry, blind-run, approval-gate and approval-phrasing replays were all performed live here, and the
two that were not are named in the reconciliation section above rather than left as `Pending`.
