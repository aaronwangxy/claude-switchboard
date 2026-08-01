# MVP Evidence

Everything below was produced on this machine against the commit stack in this
repository. Commands, exit codes, and log excerpts are copied from real runs; nothing
here is reconstructed from memory.

- Platform: macOS (darwin 25.5.0), Python 3.14.3, git 2.53.0, `claude` CLI 2.1.220
- Reviewed range: `dd39f29..HEAD` (`dd39f29` contains only the specification)

---

## 1. Environment and setup

```bash
cd claude-switchboard
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
```

Exit code `0`. `./.venv/bin/python -c "import csm, textual, pydantic, claude_agent_sdk"` →
`setup ok`.

Installed: `claude-agent-sdk`, `textual 8.2.8`, `pydantic 2.13.4`, `pyyaml`, `anyio`;
dev extras `pytest`, `pytest-asyncio`, `ruff`, `mypy`.

## 2. Launch

```bash
./.venv/bin/python -m csm                        # three-pane UI, Agent SDK backend
./.venv/bin/python -m csm --register /path/repo  # register a repository at startup
CSM_BACKEND=scripted ./.venv/bin/python -m csm   # offline demo, no model calls
```

`./.venv/bin/python -m csm --help` exits `0` and prints the `--register` / `--log-file`
options.

## 3. Verification commands and results

| Command | Exit | Result |
| --- | --- | --- |
| `PYTHONPATH=src ./.venv/bin/python -m pytest -q` | 0 | **189 passed** in ~35s |
| `./.venv/bin/ruff check src tests` | 0 | `All checks passed!` |
| `./.venv/bin/mypy` | 0 | `Success: no issues found in 34 source files` |
| real-SDK smoke (`docs/smoke-real-sdk.log`) | 0 | `REAL SMOKE COMPLETE` |
| scenario (`docs/scenario.log`) | 0 | `SCENARIO COMPLETE` |
| headless UI pilot (folded into `tests/integration/test_ui.py`) | 0 | 10 passed |

Test breakdown: 121 unit, 68 integration. Integration tests use real temporary Git
repositories; git is never mocked.

---

## 4. The three contracts used to build this prototype

Recorded before substantial implementation and verified against at the end.

### 4.1 Implementation contract

1. Python package `csm` under `src/`, layered: `domain` → `storage`/`gitops` → `core` →
   `agents`/`routing`/`workflows` → `ui`. Business logic never lives in a widget.
2. SQLite is the system of record; Pydantic models define shape and are stored as JSON
   alongside queryable columns.
3. `WorktreeService` owns every worktree operation; git runs only via argument arrays.
4. `WorkerBackend` protocol with two implementations: `SdkWorkerBackend` (real) and
   `ScriptedWorkerBackend` (deterministic, for tests and offline runs).
5. Routing is deterministic Python (`routing/router.py`); the manager model proposes and
   `validate()` refuses. Both `ModelManager` and `DeterministicManager` drive one API.
6. Workflows are declarative `WorkflowDefinition`s plus prompt templates.
7. Freshness is computed from git head/tree hashes, never from model judgment.
8. Structured artifacts reach the database only through a deterministic fenced-JSON parser.

Material decisions taken (no user input required):
- Agent SDK backend first; the `WorkerBackend` seam exists but no PTY backend is built.
- Read-only workers run in the job's writable worktree so reviewers see the change without
  owning it.
- Model IDs are configuration with `CSM_STRONG_MODEL` / `CSM_FAST_MODEL` env defaults.

Planned commit stack: scaffold → domain → storage → git → agents → workflows → core →
manager → UI → tests → evidence. The delivered stack (§5) matches, with three additional
`fix:` commits for defects found during verification and one `chore:` for lint and types.

### 4.2 Behavior contract

The 50 acceptance criteria in `CLAUDE_SESSION_MANAGER_GOAL.md` §17 are the behavior
contract verbatim. The checklist is in §8 below.

### 4.3 Evidence contract

- Every criterion is evidenced by a named test, a log excerpt, or a terminal capture.
- The deepest practical end-to-end test is a real run against the actual Agent SDK and a
  real manager model, tracing the true data and control flow (`docs/smoke-real-sdk.log`).
- Worktree behavior is evidenced by `git worktree list` output from real repositories.
- UI behavior is evidenced by headless Textual pilot assertions plus terminal captures.
- Limitations are stated rather than papered over (§10).

---

## 5. The commit stack

| Commit | Purpose |
| --- | --- |
| `b96c2fe` | Scaffold the `csm` package and dependency manifest against the locked stack. |
| `469a9a7` | Domain models, enums, the three contracts, events, and configuration. |
| `29b233d` | SQLite schema and durable store; the store is the system of record. |
| `e6adcb2` | Git runner and worktree service carrying every Git safety invariant. |
| `25a5ae9` | `WorkerBackend` protocol with the SDK and scripted implementations, and prompt composition. |
| `6f52d26` | Reusable workflow registry and Git-derived artifact freshness. |
| `a7664d7` | `SessionManager` orchestration, deterministic routing, and the attention queue. |
| `1e34b8e` | Bounded manager state snapshots and the constrained manager tool surface. |
| `7180ee9` | Unit and integration tests for routing, attention, transitions, freshness, and the full feature loop. |
| `19a041f` | Tighten types, settle lint configuration, document setup, add example config. |
| `7e5b562` | Enforce declared workflow prerequisites so implementation cannot start without an approved plan. |
| `0b447d0` | Tests for prompt composition and worker tool policy. |
| `3859d42` | Manager tools refuse malformed input instead of failing the turn. |
| `97f3d0b` | Three-pane Textual interface and application bootstrap. |
| `3351a19` | An interrupted worker stops asking for the user's attention. |
| `5f95f9c` | Drive the three-pane UI headlessly through Textual's pilot. |
| `7464aa6` | An approved plan leaves the attention queue; adds the demonstration scenario log. |
| `eeef1b9` | Describe the read-only tool policy accurately (review finding 3). |
| `f04bd51` | Invalidate artifacts through the shared freshness helpers (review finding 4). |
| _(final)_ | This evidence file: acceptance-criteria checklist and review outcome (findings 1 and 2). |

61 files and 9,233 insertions excluding this evidence file (`git diff --shortstat
dd39f29..HEAD -- . ':(exclude)MVP_EVIDENCE.md'`; the figure excludes itself so that it
stays true after edits to this file). No `WIP` or checkpoint commits; one accidental
duplicate commit was squashed before completion. The last three commits address the
independent review in §7 — two touching source, one documentation.

---

## 6. Subagents used

Three bounded helpers, each with a narrow objective, explicit non-overlapping file
ownership, and a concrete expected output. No helper ever owned a file another helper or
the primary worker was editing.

| Helper | Scope | Owned files | How its output was verified |
| --- | --- | --- | --- |
| Storage/worktree tests | Write tests for the persistence and worktree layers; report but do not fix source bugs | `tests/conftest.py`, `tests/unit/test_worktree_safety.py`, `tests/integration/test_storage_persistence.py`, `tests/integration/test_worktrees.py` | Reran its 39 tests directly; read its reported source findings and fixed two myself (see below). |
| Textual UI | Build the three-pane UI against a frozen `SessionManager` API | `src/csm/ui/*`, `src/csm/app.py`, `src/csm/__main__.py`, `docs/ui-*.txt` | Reran its headless pilot on current HEAD (all checks pass), read `app.py` and the relevant parts of `screens.py`, fixed the one mypy error it left, and folded a durable version of its pilot into `tests/integration/test_ui.py`. |
| Independent reviewer | Fresh review of `dd39f29..HEAD` with the spec, diff, commits, and evidence but not the implementer's reasoning | none (read-only) | Findings triaged in §7. |

Helper completion was never treated as evidence. Both writable helpers reported real
defects in code they did not own, which were fixed in the primary line of work:

- the test helper found that `slug()` preserved `..`, so a repository named `..` could
  place a worktree outside the managed root, and that `create_worktree` never called
  `validate_path` — both fixed in `e6adcb2`;
- the UI helper found that `interrupt_worker` left the worker's attention item open, so
  auto-advance bounced back to the worker the user had just interrupted — fixed in
  `3351a19`.

---

## 7. Fresh independent review

A fresh reviewer with no part in writing the code reviewed `dd39f29..HEAD` against the
specification, with the diff, commits, logs, and evidence but not the implementer's
reasoning. It reran the checks independently (189 passed, `ruff` clean, `mypy` clean on 34
files) and performed its own git-safety audit, confirming that the only mutating Git
invocations in the codebase are `worktree add -b`, `worktree remove` (no `--force`), and
`worktree prune`, all in `WorktreeService`, and that no `push`, `merge`, `branch -d`,
`reset`, `checkout`, `clean`, or `stash` exists anywhere. Criteria 24–28 were confirmed
directly.

It raised **four blocking findings. All four were valid and all four are fixed.**

| # | Finding | Resolution |
| --- | --- | --- |
| 1 | §9 quoted log excerpts — worktree paths, branch suffixes, commit hashes, session ids, and the `ui-03` pane — that did not appear in the files they were attributed to, while the document claimed excerpts were "copied from real runs". The quotes came from an earlier scenario run whose log was overwritten by a later one. | Re-quoted every excerpt from the current `docs/`. A sweep now checks each hash-like token, session id, branch name, and temp-dir name in this file against `docs/`, `git log`, and `src/`: **0 unsubstantiated**. The §9.4 decision example was also re-attributed from `scenario.log`, which never printed it, to `scripted_backend.py:28-40`, where it is defined and from which `ui-03` renders it. |
| 2 | Criterion 49 was ticked ✓ in §8 while §7 was an empty placeholder — the review evidence did not exist. | This section. The ✓ is now earned rather than anticipated. |
| 3 | `sdk_backend.py` claimed `READ_ONLY_TOOLS` "deliberately excludes every file-mutating tool" while including `Bash`; `prompts.py` told read-only workers "You have no file-editing tools"; and `test_read_only_workers_get_no_file_mutating_tools` named a guarantee stronger than it asserted. §10.1 already documented the truth, so code and docs disagreed. | Comment now states that `Bash` is deliberately retained and that read-only is enforced by tool policy, not a sandbox. The prompt now says file-editing tools are withheld and shell access is for inspection and tests only. The test is renamed `..._no_file_editing_tools`, documents the exception, and asserts it. `ALWAYS_DISALLOWED`'s misleading comment was corrected too: the real guarantee is structural (`mcp_servers={}`). |
| 4 | `artifacts_invalidated_by`, `is_fresh`, and `relineage` had no production callers — `_apply_invalidation` reimplemented the logic inline — so 11 of the 15 `test_freshness.py` tests covered dead code — only the four `classify_change` tests exercised live code — while the file was cited as primary evidence for criterion 34. | `_apply_invalidation` and `ready_to_push` now call the helpers; every substitution is behavior-preserving. Confirmed by mutation test: stubbing `artifacts_invalidated_by` to return an empty set now fails 5 tests **including the integration test** `test_a_code_change_invalidates_verification_and_review_and_blocks_the_push`, so the production path is genuinely covered. Reachability of the `CodeChange` members is now documented on the enum. |

Non-blocking observations were reviewed and deliberately not actioned, consistent with the
prototype's scope: the `request_cleanup` ordering race (cleanup already requires explicit
confirmation, and `can_cleanup` re-proves safety), the `_snapshot_before_change` ordering in
the new-worker branch (a fresh worker has no prior artifacts to invalidate), and
`ready_to_push` relying on `stale` flags alone when no live writable worktree exists. One
observation is recorded as a new limitation instead: the specification's §4.2 suggests
`Ctrl+Enter` to send, whereas the UI sends on `Enter` and does not list that substitution on
the help screen.

After the fixes, `pytest` (189 passed), `ruff`, and `mypy` were rerun and all pass. The
changes in findings 3 and 4 touch source, so they carry their own commits and the full
suite was rerun on the result; findings 1 and 2 are documentation-only.

### 7.1 Re-review of the fixes

Because two fixes changed source, a second fresh reviewer — with no part in the code or the
first review — verified them. It reran the checks (189 passed, `ruff` and `mypy` clean),
repeated the git-safety audit independently, and reproduced the finding-4 mutation test
exactly. It judged findings **2, 3, and 4 resolved**, and confirmed `f04bd51`
behaviour-preserving on every reachable path, checking each substitution separately.

It found finding 1 only **partially** resolved and raised two further blocking findings, both
the same defect class and both in this file. Both are now fixed:

| Finding | Resolution |
| --- | --- |
| §9.4 still claimed the decision object was "visible on screen in the right-hand pane of `ui-03`". It is not — the pane is scrolled past all but its `"blocking": true` tail. | The sentence now says exactly that, and points instead at the `[NEEDS DECISION]` marker, which genuinely is on screen. |
| §9.8's "excerpt" was a reconstruction: the two panes had been compressed independently and re-paired, so 7 of 17 rows juxtaposed text never on the same screen row — under a header promising nothing was reconstructed. | Replaced with two genuinely contiguous blocks, rows 5–12 and 27–29, truncated only on the right. A check now confirms every quoted line is an exact prefix of a real row and that the rows are consecutive; both blocks pass. |

Two further points it raised are recorded rather than fixed. Its count correction is adopted
above: 11 of the 15 `test_freshness.py` tests covered dead code, not 7 — the original figure
understated the problem. And it observed that `relineage()` also writes `tree_hash`, which
the old inline restack code did not, so `f04bd51` is behaviour-preserving but not
byte-identical in persisted state. The write is inert today — `tree_hash` is read only by
`is_fresh`'s tree branch, and no production caller passes a tree — and it stores the
semantically correct value, since that branch runs only when the tree is unchanged.

Its remaining observations were non-blocking. No unresolved blocking finding remains.

---

## 8. Acceptance-criteria checklist

All 50 criteria from `CLAUDE_SESSION_MANAGER_GOAL.md` §17. "Test" names are in
`tests/`; "log" refers to the files in `docs/`.

### Core application

| # | Criterion | ✓ | Evidence |
| --- | --- | --- | --- |
| 1 | `python -m csm` launches after documented setup | ✓ | §1–2 above; `--help` exits 0; `test_ui.py` boots the real `CsmApp` |
| 2 | UI has manager, worker list/attention, and worker panes | ✓ | `test_the_window_has_all_three_panes_and_one_manager_input`; `docs/ui-01…04` |
| 3 | A repository can be registered and persisted | ✓ | `test_storage_persistence.py`; `docs/scenario.log` §1; recovery restores it (§9) |
| 4 | Two independent workers in one repository, no shared writable worktree | ✓ | `test_two_writable_workers_in_one_repo_get_separate_worktrees`; `docs/scenario.log` §3 |
| 5 | Workers in different repositories | ✓ | `test_workers_in_different_repositories_are_independent` |
| 6 | Multiple workers stream concurrently; only the selected one occupies the right pane | ✓ | One `asyncio` pump per worker (`SessionManager._pump`); `test_one_worker_blocks_while_another_keeps_working`; the UI renders exactly one worker (`WorkerPane.show_worker`) |
| 7 | Selecting a worker restores its transcript and allows follow-ups | ✓ | `test_selecting_a_worker_restores_its_transcript`, `test_a_follow_up_through_the_worker_input_is_recorded` |
| 8 | Independent session ids, contexts, and working directories | ✓ | Phase-0 SDK spike (two live sessions, distinct ids, distinct `cwd`); `test_workers_in_different_repositories_are_independent`; `docs/scenario.log` §3 |
| 9 | Workers cannot access global registry/manager tools | ✓ | `SdkWorkerBackend._options` passes `mcp_servers={}`; `test_no_worker_is_given_manager_tools_or_registry_access`, `test_workers_never_receive_manager_tools` |
| 10 | Restart restores state; resumes or clearly marks unresumable sessions | ✓ | `test_restart_resumes_a_session_by_its_stored_id`, `test_a_worker_whose_worktree_vanished_is_marked_disconnected`, `test_a_worker_with_no_session_id_is_never_reported_as_running`; `docs/scenario.log` §9 |

### Manager and routing

| # | Criterion | ✓ | Evidence |
| --- | --- | --- | --- |
| 11 | Manager can create, list, inspect, open, message, interrupt, stop, request cleanup | ✓ | `test_the_manager_can_drive_a_worker_through_its_whole_life` exercises all fifteen tools |
| 12 | "Rebase this", "run another smoke test", "rereview it" route correctly | ✓ | `test_shorthands_route_to_the_selected_job_and_right_workflow`, `test_smoke_test_and_rereview_can_be_invoked_independently` |
| 13 | An unrelated request creates a new job/worker | ✓ | `test_unrelated_request_creates_its_own_job_rather_than_polluting_a_worker`; `docs/scenario.log` §2 |
| 14 | Ambiguous destructive operations require clarification or confirmation | ✓ | `test_destructive_requests_require_explicit_confirmation` (4 phrasings), `test_a_destructive_request_is_gated_before_the_model_is_invoked`, `test_cleanup_through_the_manager_needs_the_users_own_confirmation` |
| 15 | Manager context is bounded and built from structured state | ✓ | `test_transitions_and_snapshots.py`: worker/event/exchange caps, status-count summarisation, completed jobs excluded unless referenced, `test_snapshot_never_contains_worker_transcripts` |
| 16 | Pasting a ticket routes without a separate form or selector | ✓ | `test_pasting_a_ticket_creates_a_job_and_shows_its_worker`; the UI has exactly two inputs and no mode switch |
| 17 | Intake extracts id/title, resolves the repo, selects a bundle, starts workers, asks at most one question | ✓ | `test_pasted_ticket_creates_a_job_and_a_read_only_planner`, `test_ambiguous_repository_asks_exactly_one_question`, `test_repository_named_in_the_ticket_resolves_the_ambiguity` |
| 18 | A ticket already represented by an active job routes there, no duplicate | ✓ | `test_pasted_ticket_for_an_active_job_routes_there_instead_of_duplicating` |

### Attention workflow

| # | Criterion | ✓ | Evidence |
| --- | --- | --- | --- |
| 19 | One worker blocks while another continues | ✓ | `test_one_worker_blocks_while_another_keeps_working`; `docs/scenario.log` §4 |
| 20 | The blocked worker tops the queue with a concise reason | ✓ | `test_priority_follows_the_specified_order`, `test_blocking_plan_raises_a_prioritised_attention_item`; `docs/scenario.log` §5 |
| 21 | After the user responds, the next actionable worker opens | ✓ | `test_answering_a_blocked_worker_resumes_it_and_advances_the_queue`; `docs/scenario.log` §6 |
| 22 | Auto-advance can be paused; workers pinned and snoozed | ✓ | `test_auto_advance_can_be_paused`, `test_a_pinned_current_worker_holds_the_pane`, `test_snoozed_workers_are_hidden_until_their_snooze_expires`, `test_auto_advance_can_be_paused_and_workers_pinned` (through the UI) |
| 23 | The UI never auto-switches while the user is typing | ✓ | `test_auto_advance_never_switches_while_the_user_is_typing` and `test_the_ui_never_auto_switches_while_the_user_is_typing` (real focused `Input`) |

### Git/worktree safety

| # | Criterion | ✓ | Evidence |
| --- | --- | --- | --- |
| 24 | No two writable workers own the same worktree | ✓ | Path embeds the worker id; `assert_single_writable_owner` guards adoption; `test_two_writable_workers_in_one_repo_get_separate_worktrees`, `test_worktree_safety.py` |
| 25 | Review/question workers are read-only by default | ✓ | `READ_ONLY_ROLES` drives the default; `test_review_and_question_workers_are_read_only_by_default`, `test_a_read_only_workflow_is_refused_on_a_read_only_worker` |
| 26 | Unsafe cleanup is refused without losing work, and explains why | ✓ | `test_cleanup_requires_confirmation_and_then_refuses_unsafe_removal`, `test_cleanup_refuses_to_discard_unmerged_commits`; `docs/scenario.log` §8 |
| 27 | Safe cleanup stops the worker and removes only safe state | ✓ | `test_safe_cleanup_stops_the_worker_and_preserves_the_branch` |
| 28 | No force-push, merge, branch deletion, or discard without approval | ✓ | No code path invokes `push`, `merge`, `branch -d`, or `reset --hard`; cleanup preserves branches by construction; destructive phrasing is gated (criterion 14) |

### Contracts and feature loop

| # | Criterion | ✓ | Evidence |
| --- | --- | --- | --- |
| 29 | A feature request produces ≤10 plan lines plus structured decisions, criteria, evidence, risks, commit stack | ✓ | `test_plan_produces_all_three_contracts`; real planner output in `docs/smoke-real-sdk.log` §3–4; the cap is enforced in `_store_plan` |
| 30 | Material decisions appear as concrete choices with a recommendation | ✓ | `test_plan_produces_all_three_contracts` asserts options and recommendation; sample in §9.4 |
| 31 | Approved contracts seed a separate implementation worker without the planner transcript | ✓ | `test_the_implementer_is_seeded_with_contracts_not_the_planner_transcript` |
| 32 | Verification records criterion-specific evidence tied to current HEAD | ✓ | `test_full_feature_loop_reaches_ready_to_push_with_a_blurb`; real per-criterion commands and exit codes in `docs/smoke-real-sdk.log` §9 |
| 33 | A fresh reviewer receives contracts, diff, commits, and evidence but not implementer reasoning | ✓ | `test_the_reviewer_gets_the_diff_and_evidence_but_no_implementer_reasoning` |
| 34 | Code changes deterministically invalidate stale review/verification | ✓ | `test_freshness.py` (15 tests), `test_a_code_change_invalidates_verification_and_review_and_blocks_the_push`; `docs/scenario.log` §7 |
| 35 | No `ready_to_push` with unresolved blocking findings, stale evidence, or unmet criteria | ✓ | `test_a_blocking_review_finding_prevents_ready_to_push`, `test_verification_failure_is_recorded_honestly_and_blocks_the_push`, and the stale-evidence case above |
| 36 | The final notification has a copy-pastable blurb from stored evidence and honest limitations | ✓ | `verification_blurb` reads only stored artifacts; samples in §9.5 |

### Other workflows

| # | Criterion | ✓ | Evidence |
| --- | --- | --- | --- |
| 37 | `rebase-stack` follows configured preferences and does not force-push | ✓ | `test_rebase_uses_the_configured_preferences_and_forbids_force_push` |
| 38 | `address-review-comments` classifies every comment and fixes or explains it | ✓ | `test_address_review_comments_classifies_every_comment` |
| 39 | `smoke-test`, `full-verify`, `rereview` invocable independently after a rebase or fix | ✓ | `test_smoke_test_and_rereview_can_be_invoked_independently` (asserts a *new* reviewer each time) |
| 40 | `answer-codebase-question` runs read-only with no unnecessary worktree | ✓ | `test_a_question_runs_read_only_with_no_worktree` |

### Quality

| # | Criterion | ✓ | Evidence |
| --- | --- | --- | --- |
| 41 | Unit tests cover routing, attention priority, transitions, snapshot bounding, invalidation, cleanup safety | ✓ | `tests/unit/`: `test_routing.py` (30), `test_attention.py`, `test_transitions_and_snapshots.py`, `test_freshness.py`, `test_worktree_safety.py`, `test_prompts.py` — 121 tests |
| 42 | Integration tests cover registration, worktree creation/ownership, persistence/recovery, cleanup, with temp repos | ✓ | `tests/integration/`: `test_worktrees.py`, `test_storage_persistence.py`, `test_workers_and_recovery.py` — real git repositories throughout |
| 43 | Tests cover a full mocked feature workflow, plan through ready-to-push | ✓ | `test_feature_workflow.py`, 17 tests |
| 44 | The complete test suite passes | ✓ | 189 passed, exit 0 (§3) |
| 45 | Type hints throughout; subprocess failures surface actionable errors | ✓ | `mypy` clean on 34 files; `GitError` carries command, exit code, and stderr |
| 46 | Prompt templates enforce concise output without reducing reasoning or tools | ✓ | `test_prompts.py`: policy present for every role, `"Think and investigate as deeply as needed"` preserved, preset appended not replaced |
| 47 | Representative manager, planner, worker, verifier, reviewer responses are concise | ✓ | Real responses in `docs/smoke-real-sdk.log`: manager one sentence, planner 4 lines, implementer one sentence, verifier verdict-first, reviewer `pass` with no filler |
| 48 | Delivered as a coherent atomic commit stack, each commit listed with its purpose | ✓ | §5 |
| 49 | Bounded subagents used, with a fresh independent final review and no overlapping writable ownership | ✓ | §6 and §7 |
| 50 | Contracts recorded before substantial implementation and verified against | ✓ | §4, verified by this checklist |


---

## 9. Demonstrated scenarios

Full logs: `docs/scenario.log` (deterministic backend) and `docs/smoke-real-sdk.log`
(real Agent SDK and real manager model).

`docs/scenario.log` predates the freshness refactor in `f04bd51`, which touches the
invalidation behaviour the log demonstrates, so the scenario was rerun on the final HEAD
and the two outputs compared with commit hashes, temporary paths, and session ids
normalised. They are identical, confirming the committed log still describes current
behaviour; the raw identifiers differ only because each run creates fresh temporary
repositories.

`docs/smoke-real-sdk.log` predates `eeef1b9`, which reworded `READ_ONLY_NOTE`. It was not
regenerated: a rerun costs real model calls and is non-deterministic, so a fresh log could
not be diffed against it the way the scenario was. The change is a wording clarification to
one prompt note — the tool policy it describes is unchanged, and `tests/unit/test_prompts.py`
covers the composition — but that log is evidence produced by the immediately preceding
revision of that prompt, not by the exact final HEAD.

### 9.1 Two independent workers in separate worktrees

From `docs/scenario.log` §3 — two unrelated tickets pasted into the one manager input,
each producing its own job and its own writable worker in the same repository:

```text
  ENG-118 · implementer   session=scripted-9acc28c   branch=csm/eng-118-c2feb7d5
                          cwd=.../worktrees/payments/eng-118-implementer-c2feb7d5
  ENG-204 · implementer   session=scripted-fe036b9   branch=csm/eng-204-48b6ac72
                          cwd=.../worktrees/payments/eng-204-implementer-48b6ac72

$ git worktree list
/private/var/.../repo-payments-0noms24x                          c756baf [main]
/private/var/.../worktrees/payments/eng-118-implementer-c2feb7d5 c756baf [csm/eng-118-c2feb7d5]
/private/var/.../worktrees/payments/eng-204-implementer-48b6ac72 c756baf [csm/eng-204-48b6ac72]
```

(Long temporary paths are elided with `...`; identifiers are verbatim.)

Distinct worktrees, distinct branches, distinct session ids, and neither worktree is
inside the user's source repository.

The real-SDK run shows the same from the other direction (`docs/smoke-real-sdk.log` §6):
one managed worktree holding two real commits produced by a real implementation worker.

### 9.2 A blocked worker while another continues

From `docs/scenario.log` §4:

```text
  ENG-118 · implementer   status=blocked  waiting_for=[NEEDS INPUT] Should an expired
                          refresh token log the user out, or retry once with the old token?
  ENG-204 · implementer   status=idle     waiting_for=None
```

The blocked worker sits at the top of the queue with a concise reason (§5 of the log):

```text
  [human_decision  ] ENG-118 · implementer   [NEEDS INPUT] Should an expired refresh token …
  [ready_to_push   ] ENG-204 · implementer   ENG-204 cache fix is ready to push.
```

### 9.3 Response followed by attention auto-advance

From `docs/scenario.log` §6:

```text
  selected before: ENG-118 · implementer
  auth worker is now: idle
  queue now: [('ready_to_push', 'ENG-204 · implementer')]
  auto-advance opens: ENG-204 · implementer
  ...but while the user is typing: None (the pane does not move)
```

### 9.4 A concise contract set

Produced by a real planner session against a real repository
(`docs/smoke-real-sdk.log` §3–§4). The user-facing plan:

```text
## Plan: Add greeting helper

**Summary**
1. Create `greet.py` with single `greet(name)` function
2. Strip whitespace; if empty, return "Hello, there!", otherwise return "Hello, <name>!"
3. Create `test_greet.py` with unit tests covering both acceptance cases
4. Two commits: implementation, then tests

**Decisions**
No blocking decisions — the spec is clear and straightforward.
```

The structured contract stored alongside it: 4 plan lines (cap is 10), a 2-commit stack,
0 blocking decisions, and acceptance criteria `AC1, AC2, AC3, SMOKE`.

The scripted planner (`src/csm/agents/scripted_backend.py:28-40`) shows the decision shape
when one is needed — concrete options, a recommendation, and `blocking: true`. It is
quoted here from that source. In `docs/ui-03-two-workers.txt` the worker pane is scrolled
past most of this object — only its `"blocking": true` tail is on screen — but the
`[NEEDS DECISION] Choose the legacy-write strategy.` marker it produces is visible there,
and it is what leaves `ENG-421` blocked in the worker list:

```json
{"id": "D1", "question": "Must legacy records remain writable?",
 "options": ["Yes, keep legacy writes", "Read legacy, write new format only",
             "Drop legacy support"],
 "recommendation": "Read legacy, write new format only", "blocking": true}
```

### 9.5 Independent review and invalidation after a code change

From `docs/scenario.log` §7 — evidence recorded against an exact head, then discarded the
moment the tree changed:

```text
  verification tested head aa5f890b  stale=False
  review       reviewed head aa5f890b  verdict=pass  stale=False
  ready_to_push=True  blockers=[]

  now the implementer changes code again:
  verification stale=True (implementation_edit at 310fe85f)
  review       stale=True (implementation_edit at 310fe85f)
  ready_to_push=False  blockers=['Verification does not apply to current HEAD.',
                                 'Review does not apply to current HEAD.']
```

The copy-pastable blurb, generated from stored evidence only:

```text
Verification performed:
- AC1: passed — Preferences survived a restart of the store. [`pytest -q` (exit 0)]
- Tested head: aa5f890bc039
- Independent review of aa5f890bc039: pass, 0 open finding(s).

Limitations:
- None recorded.
```

The real-SDK run produced the same shape from a real verifier's own commands
(`docs/smoke-real-sdk.log` §9), including the exact `pytest test_greet.py -v` invocation
and its exit code for each criterion.

### 9.6 Cleanup safety

From `docs/scenario.log` §8:

```text
  unconfirmed: performed=False — Cleanup is destructive and needs explicit confirmation.
  confirmed:   performed=False — 2 commit(s) on csm/eng-204-48b6ac72 are not reachable
                                 from main and have not been acknowledged as disposable.
  clean worktree: performed=True — Cleaned up 1 worker(s); branches preserved.
  branch csm/eng-118-c2feb7d5 still exists: True
```

### 9.7 Restart recovery

From `docs/scenario.log` §9 — a new process, a new backend, the same database:

```text
  recovery notes: ['ENG-118 · planner: resumed', 'ENG-204 · planner: resumed',
                   'ENG-204 · implementer: resumed', 'ENG-204 · verifier: resumed',
                   'ENG-204 · reviewer: resumed']
  jobs restored: ['ENG-204', 'ENG-118']
  decisions restored: 1
```

A worker whose worktree has vanished, or which never captured a session id, is marked
`disconnected` with an actionable explanation rather than being reported as running
(`tests/integration/test_workers_and_recovery.py`).

### 9.8 Terminal captures of the three-pane UI

`docs/ui-01-startup.txt`, `docs/ui-02-blocked-planner.txt`, `docs/ui-03-two-workers.txt`,
`docs/ui-04-help.txt`. Rows 5–12 of `ui-03`, contiguous and truncated only on the right
(the file is wider than this page; nothing is dropped vertically or re-paired):

```text
╭─────────────────────────────────────────────────────────────╮╭────────────────────────
│ Manager                                                     ││ ENG-999 · Planner · Idl
│ persist and the gateway honours them. This is a second…     ││             "blocking":
│ mgr  Started ENG-999 in a new job. Planning is in           ││           }
│ progress.                                                   ││         ],
│                                                             ││         "commit_stack":
│ mgr  ENG-421 · planner is pinned.                           ││           {
│                                                             ││             "order": 1,
```

and rows 27–29, the worker list:

```text
│ Workers (2)  ·  auto-advance on                             ││           {
│ · ENG-421 planner  blocked  [NEEDS DECISION] Choose the lega││             "id": "AC1"
│ ✓ ENG-999 planner  idle  planner · planning · repo-alpha-gy8││             "behavior":
```

Elsewhere in the same capture the manager log shows pin, unpin, auto-advance off and on,
and snooze, all driven through the one manager input (criterion 22). `ENG-421` is `blocked`
but carries no attention marker — `_marker` in `src/csm/ui/screens.py` returns `·`, not `!`
— because it was interrupted, and an interrupted worker stops asking for attention
(`3351a19`). The pane state and the queue state agree.

---

## 10. Known limitations and external blockers

These are deliberate. Each is recorded rather than solved, per the prototype's scope.

1. **Read-only is enforced by tool policy, not by a sandbox.** Read-only workers are
   denied `Edit`, `Write`, `NotebookEdit`, and `MultiEdit`, but they retain `Bash` because
   reviewers and verifiers need `git log`, `git diff`, and test commands. A worker that
   deliberately wrote through `Bash` would not be stopped. A filesystem sandbox is the
   real fix and is out of scope here.
2. **Verifiers dirty the worktree they inspect.** Read-only verifiers and reviewers run in
   the implementer's worktree so they see the change under review. Running the test suite
   there creates `__pycache__`, which the ready-to-push gate then correctly reports as an
   uncommitted change — visible in `docs/smoke-real-sdk.log` §9, where a genuinely complete
   change is held back by exactly that. The gate is behaving correctly; the ergonomics are
   not. A separate read-only checkout per verifier would fix it.
3. **Blocked detection relies on a marker.** A worker signals that it needs the user by
   ending its reply with `[NEEDS INPUT]` or `[NEEDS DECISION]`. A worker that asks a
   question without the marker is recorded as idle, not blocked. The SDK offers no
   structured "awaiting user" signal for this shape of session.
4. **Permission prompts are not surfaced interactively.** Writable workers run with
   `permission_mode="bypassPermissions"` inside their own isolated worktree. The
   `permission_required` event type and its attention priority exist and are wired, but no
   code path currently emits it. An interactive permission flow is deferred.
5. **`had_conflicts` is never set from a real rebase.** `classify_change` accepts it and
   `CodeChange.REBASE_WITH_CONFLICTS` / `CLEAN_REBASE` are implemented and unit-tested, but
   the rebase workflow does not yet parse git's conflict state. In practice a rebase that
   changes the tree is classified as an implementation edit, which invalidates the same
   artifacts — the conservative direction.
6. **The manager model can still choose a worse route than the deterministic one.** The
   route proposal is advisory to the model; only the safety invariants are mandatory. The
   first real-SDK run showed the model trying to skip planning, which the prerequisite gate
   refused (`7e5b562`). Refusals bound the damage; they do not guarantee an optimal route.
7. **No `git worktree` locking.** Two `csm` processes against the same data directory could
   race. Single-process personal use is the documented operating path.
8. **Manager turn latency is not streamed.** The manager's reply appears when the turn
   finishes. Worker output streams; the manager's does not.
9. **Send is `Enter`, not `Ctrl+Enter`.** The specification's §4.2 suggests `Ctrl+Enter` to
   send. Both inputs send on `Enter`, which is Textual's natural `Input` behavior and means
   a message cannot contain a newline typed directly — pasted multi-line tickets are
   unaffected, which is the case that matters. Unlike the `Ctrl+O` and `Ctrl+P`
   substitutions, this one is not listed on the help screen.

## 11. Deliberately deferred non-goals

Named in the specification as out of scope, and not built: a chat room for agents;
unlimited agent fan-out; automatic merging, force-pushing, or destructive cleanup; a
multi-user or remote orchestration service; a plugin marketplace; a generic workflow
engine. Also not built, by choice: a native `claude` CLI/PTY backend (the `WorkerBackend`
seam exists for it), and any Electron/React/TypeScript/Node dependency.

## 12. Assumptions made

1. `~/.local/share/claude-session-manager/` is the data root, overridable with `CSM_HOME`;
   `~/.config/claude-session-manager/config.yaml` is the config path, overridable with
   `CSM_CONFIG`. Nothing is written inside the user's source repository.
2. `setting_sources` defaults to `["user", "project"]`, so worker sessions do load the
   repository's `CLAUDE.md` and the user's Claude settings. This is documented in
   `config.example.yaml` and can be set to `[]`.
3. Structured artifacts are parsed from a fenced ```json block in the worker's reply. The
   SDK's typed structured-output mode would be stricter; the fenced block keeps the same
   worker usable for prose and artifacts in one turn.
4. Cleanup never deletes a branch, even when the user confirms. Branch deletion is a
   separate explicit action and is not implemented.
5. `Ctrl+C` is Textual's own binding, so interrupt is `Ctrl+O`; the command palette is
   disabled so `Ctrl+P` can pin. Both substitutions are listed on the help screen.
6. The scripted backend is a first-class operating mode (`CSM_BACKEND=scripted`), not just
   test scaffolding, so the whole control plane can be demonstrated without model calls.
