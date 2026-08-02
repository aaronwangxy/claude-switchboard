# The Manager

The Manager is one persistent native Claude Code process running under the same tmux
runtime supervisor as workers. It routes, summarises, and proposes. It is never the system
of record and it never writes code.

Two implementations share one `handle(text) -> str` contract:

- `PersistentNativeManager` — production. A generation-bound native Claude process with a
  constrained Switchboard MCP.
- `DeterministicManager` — rules only, used by `SB_BACKEND=scripted` for offline runs and
  routing tests. It also answers `status()`, `enter()`, and `release_human()`, so the board
  never has to ask which kind it is holding.

## Identity, generation, and authority

`manager.identity` is a stable UUID in SQLite. Each replaceable Claude process gets a
generation-numbered `RuntimeInstance` with an exact tmux binding, a Claude session UUID, a
launch fingerprint, an owner, and a process state.

**Only the newest generation may act.** Every MCP tool call reloads current durable state
and refuses unless the runtime UUID and generation match, `agent_kind` is `manager`, and
the runtime has not exited. Human ownership closes autonomous input but deliberately keeps
that same live generation's MCP usable. Rotation marks the old runtime exited before
terminating its tmux target and saving the new generation.

The generation row is created inside a `BEGIN IMMEDIATE` transaction, so it acts as an
authority lease: a second board process observes it rather than minting a peer.

## The MCP boundary

Claude launches a real stdio MCP subprocess. It is a narrow proxy over a mode-0600,
generation-specific Unix socket to the board's authoritative in-process `SessionManager`;
it never constructs a peer orchestrator or its own worker pumps. Handlers call
`SessionManager`, so the manager has no direct database or repository access.

The tools are semantic orchestration operations only:

`register_repository`, `create_job`, `inspect_state`, `list_workflows`, `start_workflow`,
`start_run`, `send_worker_followup`, `record_decision`, `resume_run`, `interrupt_worker`,
`stop_worker`, `inspect_contracts`, `approve_plan`, `status_summary`.

Two of them require the user's own current turn to have said so:

- `stop_worker` needs an explicit confirmation in the current message;
- `approve_plan` needs an approval in the current message.

The approval guard is a lexical one, and it is honest about that: the durable property it
enforces is that the approval appeared in the user's own current turn, not that a model
understood it. The approval must open its own sentence in the user's voice, any withholding
instruction anywhere in the message vetoes it, quoted text is somebody else's approval being
relayed, and an interrogative never grants.

Its launch disables native coding tools and allows only `mcp__switchboard__*`. **Workers
never receive the manager's MCP configuration, socket, or launch arguments** — orchestration
authority is structurally unreachable from a worker, not merely discouraged.

A malformed tool call returns a refusal, never an exception that kills the turn.

### Surviving a board restart

The board owns the Unix socket, so quitting it closes the manager's MCP connection. Claude
Code never respawns a stdio MCP server that exits, so dying there would silently strip a
surviving Manager of every orchestration tool for the rest of its session — leaving a model
that narrates tool calls it cannot make. The bridge therefore reconnects: the board rebinds
the same generation-bound path when it returns, and every call is reauthorised server-side.

The bridge is exactly-once in every realistic case, and says so honestly when it is not: a
call the board accepted before vanishing is reported as *applied-unknown*, so the Manager
inspects state rather than retrying a mutation that may already have happened.

## Workspace and configuration

The Manager's cwd is `<SB_HOME>/manager-workspace`, which contains only a marker file and
is refused if it lies beneath Git metadata. It receives ordinary native Claude behaviour
plus an additive Switchboard manager prompt.

It uses the same configured executable, wrapper, and environment as workers, and the same
native user and managed/company discovery — but its clean non-repository cwd means it picks
up no project or project-local repository context. User authentication, user settings,
managed policy, hooks, plugins, and skills remain discoverable by Claude itself.

It launches with `--strict-mcp-config`: arbitrary user or project MCP tools would violate
structural manager isolation, so only the generation-bound Switchboard server is loaded.
Admin-managed policy is enforced by Claude and is never bypassed. If an installation
mandates an incompatible managed MCP, Claude must reject the launch rather than Switchboard
weakening that policy.

## Context and rotation

The native transcript is replaceable working memory; SQLite is long-term orchestration
memory. A fresh Manager generation reconstructs bounded objectives, jobs, runs, workers,
attention, decisions, contracts, and evidence through `inspect_state` and
`inspect_contracts`. Worker transcripts are not fed to it.

Rotation stores at most 4,000 characters across six compact handoff fields, for nuance that
is genuinely not durable elsewhere — an objective, an unresolved decision, a correction —
and then starts a fresh Claude session. It is triggered by an explicit "fresh manager"
request, by a bounded 80-turn context-health limit, or by crash recovery replacing an
unrecoverable process.

Losing the Manager transcript cannot change workflow correctness. That is the point.

## Entering the Manager

Selecting Manager and pressing Enter claims human ownership and attaches to its existing
tmux target, exactly as for a worker. It never uses `claude --resume`. Handback requires
explicit confirmation that the composer is empty. Ordinary board navigation never stops the
process.
