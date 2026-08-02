# Phase 8 adversarial dogfood report

This is the durable record for Phase 8. Findings are recorded from public, user-facing
behavior before implementation inspection or repair. Diagnosis, resolution, regression
coverage, and replay results are appended only after the original reproduction exists.

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

Reproducibility: Once; deterministic replay pending.

Workaround: Submit `fresh manager` and wait through the full native startup boundary.

Diagnosis: Pending implementation inspection.

Resolution:
On recovery, a ready runtime now resolves only obsolete native `permission required` attention;
workflow decisions and plan approvals remain actionable.

Regression coverage:
Recovery integration coverage seeds both permission and plan-approval items, adopts the ready
runtime, and asserts only plan approval survives. The full real-tmux suite passes.

Replay result:
The first public replay exposed and rejected an over-broad implementation that cleared approval
too. The narrowed fix passes deterministic and real-tmux replay; a fresh public approval-boundary
replay remains pending.

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

Reproducibility: Once; deterministic UI replay pending.

Workaround:
Use the input's line-edit command to clear the stale value before typing another request.

Diagnosis:
Rejected as a product defect. The reproduction injected the handback answer and next command in
one low-level PTY write across Textual's terminal-mode handoff. Normal separated submissions clear
immediately, including after exact-session entry and controller recovery.

Resolution: No product change.

Regression coverage: Pending.

Replay result: Pending.

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

Diagnosis: Pending until black-box reproduction is complete.

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

Reproducibility: Once; deterministic replay pending.

Workaround:
Enter the worker, verify its composer is empty, detach, and confirm handback.

Diagnosis:
Recovery mapped a ready runtime to an idle worker but resolved no attention, leaving a delivered
native permission notification actionable after the native prompt had disappeared.

Resolution:
On recovery, a ready runtime now resolves only obsolete native `permission required` attention;
workflow decisions and plan approvals remain actionable.

Regression coverage:
Recovery integration coverage seeds both permission and plan-approval items, adopts the ready
runtime, and asserts only plan approval survives. The full real-tmux suite passes.

Replay result:
The first public replay exposed and rejected an over-broad implementation that cleared approval
too. The narrowed fix passes deterministic and real-tmux replay; a fresh public approval-boundary
replay remains pending.

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
Latest objective/status visibility passed after controller restart. A clean rapid-follow-up replay
remains pending because the live controller cannot be restarted again after the environment's
approval quota was exhausted.

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
Pending a controller restart so a fresh Manager can receive the revised system prompt. The restart
requires tmux access, and the environment rejected further escalation after its usage quota was
exhausted.

## Scenario log

Scenarios and exact replay results will be recorded as they are exercised.

## Comparative test

Pending.

## Independent replay and review

Pending.
