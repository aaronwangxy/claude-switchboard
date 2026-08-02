# Phase 7 product integration evidence

Phase 7 made the board session-first: Manager and workers share one session list; the selected
session shows durable workflow, lifecycle, ownership, blocker, worktree/branch, authoritative
lineage, run step, and evidence state. Claude Code owns conversation rendering. Highlighting a
session and pressing Enter enters its exact native process; `Ctrl+E` remains an alternate key.

## Deterministic and native-tmux coverage

The integration suite uses disposable Git repositories. Scripted and fake-native tests cover
simple routing, complete-ticket approval/implementation/verification/review/finalization,
parallel isolated worktrees, human intervention and replay, Manager MCP, lower-layer
exact-process entry/handback, composite restart recovery, and exact-generation tmux adoption
without paid calls. These scenarios exercise the same control plane but are not all driven through
Textual. Phase 7 added regressions for foreign-tmux ownership rollback,
long Unix socket paths, duplicate workflow startup, startup attention, and completed-turn
handback.

## Authenticated native-Claude smoke

The real executable was `/Users/aaron/.local/bin/claude`, Claude Code 2.1.220. Every paid turn used
the `haiku` alias, observed as `claude-haiku-4-5-20251001`. Testing used isolated `SB_HOME` state
and disposable repositories. Approximate paid scope was five tiny Manager turns while diagnosing
first-use startup behavior and one planner turn; no implementation, verification, review, model
quality comparison, Sonnet, or Opus run was performed.

Observed successful path:

- native Manager authenticated, loaded in a clean non-repository workspace, and used only the
  generation-bound Switchboard MCP;
- Manager created a job, corrected an atomic/composite tool choice after a refusal, selected
  `plan-feature`, launched one native planner, and accurately reported a startup blocker;
- the Haiku planner completed a managed turn and produced current implementation and behavior
  contracts for the disposable repository;
- Manager and worker exact-process entry returned the same Claude session IDs; handback restored
  manager ownership;
- a fresh controller adopted the exact worker runtime and session without creating a peer;
- a live Manager launch mismatch now refuses recovery rather than minting a peer generation.

Configuration findings:

- workers used normal repository cwd, native plan mode for the read-only planner, Haiku, and the
  configured executable/environment;
- Manager used the same executable/authentication and Haiku from an isolated workspace outside
  Git; its real transcript recorded Switchboard MCP calls and no arbitrary repository tools;
- native managed/company policy was not weakened or bypassed;
- a newly created Manager workspace and a newly seen disposable repository each required Claude's
  native trust confirmation once. Switchboard now presents a starting-session entry instruction;
- this installation took about 31 seconds to emit `SessionStart`, exposing and fixing the former
  30-second false timeout;
- a controller restart adopted the worker exactly. A later paid-state Manager restart exposed a
  launch mismatch that could create a peer generation; recovery now refuses that unsafe case and
  tells the user to resolve the existing Manager explicitly.

## Failures found and fixed

- deeply nested `SB_HOME` paths exceeded macOS Unix-socket limits;
- foreign tmux refusal could strand ownership as human;
- first-use trust could look like a failed/hung worker and invite a duplicate workflow start;
- a readiness hook racing the timeout could be overwritten;
- completed Manager/worker turns could be acknowledged twice during human handback;
- current docs and agent instructions still described SDK/resume-era entry and prompting.

## Phase 8 targets

Attack first-use trust and authentication prompts interactively; delayed hooks around the
60-second bound; resolving a refused Manager launch mismatch under the real environment; process
cleanup after controller loss; repeated enter/watch/type/detach cycles during active tools and
permissions; and whether the sparse board explains multi-job dependencies once several real
workers are active. Parallel and full-ticket cases should remain deterministic unless a specific
native integration invariant is in doubt.
