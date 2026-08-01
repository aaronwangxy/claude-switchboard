# Phase 5 native composite evidence

Native workers advertise composite support. The existing `WorkflowRun` state machine,
approvals, prerequisites, bounded repeats, contracts, evidence, freshness, and worktree
ownership remain the orchestration model.

Advancement authority is explicit and durable:

- reserving a worker and sending a prompt do not complete a step;
- only a successfully applied, manager-owned terminal event marks it complete;
- artifact harvesting and that completion marker share the hook-application transaction;
- recovery adopts the exact live runtime and advances a marked step once;
- loss of an incomplete runtime blocks rather than resending or advancing;
- failed turns never set completion authority;
- human ownership taints the attempt, pauses the run, and explicit resume replays the same
  bounded step from durable contracts without consuming an iteration.

Each job stores one authoritative worktree. The first writable worker establishes the
lineage, and `set_authoritative_worktree` is the explicit reassignment operation. All Git
inspection used by review prompts, verification placement, freshness, invalidation, and
ready-to-push resolves through that identifier. A mutating workflow cannot target another
writable worker accidentally.

Deterministic integration tests cover approval gates, implementation/verification/review,
blocking-review repair and bounded repetition, attach/detach, and lineage selection. The
real-tmux suite uses `fake_native_claude.py` to exercise supported hook subprocesses and
proves a complete native run, terminal-event/artifact progression, restart adoption after
durable completion, human intervention and replay, failed-turn blocking, permissions, and
native atomic parity without paid Claude calls.
