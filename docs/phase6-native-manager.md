# Phase 6 native manager

The production manager is one persistent native Claude Code process under the same tmux
runtime supervisor as workers. `manager.identity` is stable in SQLite; each replaceable
Claude process has a generation-numbered `RuntimeInstance`, exact tmux binding, Claude
session UUID, launch fingerprint, owner, and process state.

## Authority and MCP isolation

Only the newest manager generation may act. Every tool call reloads current durable state
and refuses unless the runtime UUID and generation match, `agent_kind=manager`, and the
runtime is not exited. Human ownership closes autonomous input but deliberately keeps that
same live generation's MCP usable. Rotation marks the old runtime exited before terminating
its tmux target and saving the new generation. Workers never receive the manager MCP config,
socket, or launch arguments.

Claude launches a real stdio MCP subprocess. It is a narrow proxy over a mode-0600,
generation-specific Unix socket to the board's authoritative in-process `SessionManager`;
it never constructs a peer orchestrator or worker pumps. The MCP exposes bounded semantic
orchestration operations: inspect state, list workflows,
start atomic/composite workflows, follow up/interupt/stop workers, inspect contracts,
approve plans, and report status. Handlers call `SessionManager`; the native manager has no
direct database or repository tools. Its launch disables native coding tools and allows
only `mcp__switchboard__*`.

## Context, memory, and rotation

The manager cwd is `<SB_HOME>/manager-workspace`, which contains only a marker and is
rejected if it lies beneath Git metadata. It receives the ordinary native Claude system
behavior plus an additive Switchboard manager prompt. A fresh generation reconstructs
jobs, runs, workers, attention, contracts, and recent decisions through bounded MCP
responses. Worker transcripts are not included.

Switchboard state is long-term memory; the Claude transcript is working memory. Rotation
stores at most 4,000 characters across six compact handoff fields for objective, unresolved
decisions, rationale, questions, or corrections not otherwise durable. It then starts a
fresh Claude session. Explicit "fresh manager" requests and a bounded 80-turn context-health
limit trigger rotation; crash recovery replaces an unrecoverable process. A natural completed
goal boundary is also a safe point for an explicit fresh manager, without making transcript
interpretation authoritative. Losing the manager transcript cannot change workflow correctness.

Entering Manager claims human ownership and attaches to the existing tmux target. It never
uses `claude --resume`. Handback requires explicit confirmation that the composer is empty.
Normal board navigation never stops the process.

## Native configuration findings

Manager and workers use the same configured executable/wrapper and `claude.env`; neither
bypasses managed policy. Worker launches remain unchanged and perform normal discovery in
their repository/worktree, including project and local instructions/settings.

The manager also uses native discovery, but its clean non-repository cwd prevents project
and project-local repository context. User authentication, user settings, managed/company
policy, wrapper/proxy environment, hooks, plugins, and skills/commands remain discoverable by
native Claude. The manager uses `--strict-mcp-config`: arbitrary user/project MCP tools would
violate structural manager isolation, so only the generation-bound Switchboard server is
loaded. Admin-managed policy remains enforced by Claude and is never bypassed. If an
installation mandates an incompatible managed MCP, Claude must reject the launch rather than
Switchboard weakening that policy. The overlay adds Switchboard lifecycle hooks; it does not
replace settings or choose a bypass permission mode.

The fake-native tmux suite verifies exact launch arguments, process adoption, rotation,
generation revocation, and same-process entry without paid execution. The previously observed
real CLI behavior is documented in `native-claude-prototype.md`. Authentication was expired in
that experiment, so a successful paid manager turn and an environment-specific audit of every
managed hook/plugin/skill/MCP remains a dogfooding check; Switchboard does not claim those were
individually exercised.
