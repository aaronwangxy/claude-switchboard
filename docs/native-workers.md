# Production native workers

Production worker calls follow one path:

```text
SessionManager
  -> NativeClaudeBackend
  -> NativeClaudeRuntime / TmuxRuntimeSupervisor
  -> tmux generation-bound pane
  -> native Claude Code
  -> supported lifecycle hooks
  -> durable native turn + normalized WorkerEvent
  -> contracts, evidence, freshness, workflow completion
```

`SB_BACKEND=scripted` selects `ScriptedWorkerBackend` for deterministic tests and offline
demos. There is no SDK worker fallback. Phase 6 also migrated the manager to native Claude,
so `claude-agent-sdk` is no longer a dependency.

## Launch and configuration

Workers execute the configured `claude.executable`, or the executable found on PATH, in the
repository/worktree assigned by `SessionManager`. Claude receives a preassigned durable
session UUID, the role/workflow system-prompt append, an optional configured model, and a
private Switchboard hook overlay. The launch deliberately omits `--setting-sources`, so native
Claude performs its normal user, managed/company, project, and project-local configuration
discovery. Writable workers retain native permission policy; read-only roles launch in native
`plan` permission mode so Claude withholds editing operations while preserving managed/company
policy. Switchboard never sets a bypass mode or supplies the manager's in-process MCP server.

The launch fingerprint binds the executable, cwd, model, worker prompt, environment additions,
hook interpreter/event set/database, and state directory. tmux additionally binds runtime UUID,
generation, fingerprint, session/pane identity, and pane PID.

## Turns and workflow safety

Every manager prompt owns a durable single-input lane before tmux injection. A random token and
turn UUID correlate `UserPromptSubmit` to Claude's `prompt_id`; later hooks use that prompt ID.
Only a turn whose durable origin is `MANAGED` becomes a `WorkerEvent`. Human prompts and their
`Stop` events remain recorded but cannot harvest artifacts or advance workflows.

`Stop.last_assistant_message` is the final result. It flows through the same
`SessionManager._finish_turn` and fenced-JSON contract parsers used by scripted tests. The hook
delivery ledger is checked and marked by `SessionManager` using the hook event UUID, so a replay
after controller restart does not duplicate transcripts, artifacts, or run advancement. Terminal
lane reconciliation also repairs a crash after orchestration application but before turn
acknowledgement.

## Recovery and entry

Recovery observes the exact generation-bound tmux target first. A matching live process is
adopted, with the same PID and Claude session UUID. A stale target is rejected. An absent/exited
process creates a new runtime generation and a fresh native Claude session reconstructed from
durable Switchboard state; it is not ordinary entry and does not make Claude history
authoritative.

Entering a worker claims human ownership and returns `tmux attach-session` for the existing
target. It never invokes `claude --resume`, never replaces the process, and does not interrupt an
active turn. Entering while a managed turn is active durably taints that turn as human-intervened,
so its result cannot advance a workflow. The UI waits for the external tmux client off its event
loop, so hook processing continues while the user observes or interacts. Manager handback
requires an explicit confirmation that the user cleared all unsubmitted composer text; tmux
cannot prove composer state.

## Current boundary

Atomic workflow completion is enabled. Composite workflow migration/validation is intentionally
deferred. Native interruption remains conservative: key delivery creates
`INTERRUPT_REQUESTED`, not successful completion, and keeps the input lane closed until a
supported lifecycle event or explicit recovery resolves it.
