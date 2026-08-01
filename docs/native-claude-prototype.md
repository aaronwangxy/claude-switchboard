# Native Claude prototype (Phase 3)

Phase 3 adds an experimental `NativeClaudePrototype`; it is deliberately not a
`WorkerBackend` and cannot advance workflows. It runs the configured Claude executable in
the Phase 2 tmux substrate and treats Claude hooks, not pane contents, as lifecycle evidence.

## Identity and launch

The durable runtime's launch fingerprint hashes the adapter protocol version, resolved
executable, resolved cwd, configured setting sources, and configured environment additions.
The Phase 2 tmux metadata also binds runtime UUID, generation, and that fingerprint to the
exact pane PID. A Claude session UUID is allocated before launch, persisted, and passed with
`--session-id`. Recovery reuses it. A hook carrying a different session UUID is rejected.

The adapter supplies a mode-0600 settings overlay with command hooks for `SessionStart`,
`UserPromptSubmit`, tool events, `PermissionRequest`, `Notification`, `Stop`, `StopFailure`,
`InstructionsLoaded`, and `SessionEnd`. This overlay adds Switchboard hooks while
`--setting-sources` controls normal user/project/local settings. It does not change Claude's
permission mode or bypass managed policy.

## Turn provenance and lifecycle

Before injection, Switchboard durably creates a `PENDING` turn with a random 256-bit token.
It appends an HTML comment containing the turn UUID and token, then uses tmux's literal
buffer/paste path. `UserPromptSubmit` validates both values and binds Claude's `prompt_id` to
that turn. A prompt without a valid pending marker creates a `HUMAN` turn. Later events use
the Claude `prompt_id`, so a human turn cannot complete an earlier managed turn. The marker
is intentionally stronger than prompt-text equality, though Claude can see it; a future
Claude-supported client metadata field would be preferable if one becomes available.

`PermissionRequest` and permission/elicitation notifications move a turn to waiting.
`Stop` stores `last_assistant_message`; `StopFailure` stores its error and optional last
message. Completion remains pending until the controller acknowledges the terminal turn.
The existing fenced-JSON artifact parser consumes the stored `last_assistant_message`.
Claude documents that user interruption does not emit `Stop`. A Switchboard-issued Ctrl-C is
therefore recorded only as `INTERRUPT_REQUESTED`; it does not make the runtime reusable.
Confirmation and recovery remain deliberately unresolved rather than inferred from terminal
idle state.

## 2026-08-01 real CLI experiment

The tested executable was `/Users/aaron/.local/bin/claude`, resolved from PATH because
`claude.executable` was unset. It reported Claude Code 2.1.220, native install commit
`4073f59596e2`. The process ran in an isolated git repository at
`/private/tmp/switchboard-native-phase3/repo`.

Observed hook payloads from the real process:

- `SessionStart`: the preassigned session UUID, transcript path, exact cwd, source `startup`,
  and model `claude-opus-5`.
- `InstructionsLoaded`: the probe repository's exact `CLAUDE.md` path, `Project` memory type,
  and `session_start` reason.
- `UserPromptSubmit`: exact multiline prompt, Unicode, quotes and shell metacharacters once;
  permission mode `default`; and a Claude-generated `prompt_id`.
- `StopFailure`: the same session and prompt IDs, `authentication_failed`, and
  `last_assistant_message` saying login had expired.
- `Notification`: `idle_prompt` with the same session and latest prompt IDs after failure.
- `SessionEnd`: reason `prompt_input_exit` after `/exit`; durable runtime became `EXITED`.

The CLI's `/status` screen showed the exact persisted session UUID and cwd. It reported
`User settings, Shared project settings, Command line arguments` for the Switchboard launch.
A separate manual launch in the same repository reported `User settings, Shared project
settings, Project local settings`. Thus the current Switchboard default
`setting_sources=[user, project]` intentionally inherits user and shared project settings but
differs from a manual launch by excluding local settings. Managed settings remain enforced by
Claude itself, but no managed-settings source was displayed in this environment. The successful
startup establishes executable/wrapper environment and settings loading; the expired login
establishes that authentication state was inherited, not that it was usable. MCP servers,
plugins, skills/commands, and hooks other than the overlay were not individually exercised.

Human entry attached a PTY client to the same pane PID, displayed the normal Claude UI, and
successfully opened `/status`. A prompt typed through that attached client was classified
`HUMAN` with its own Claude prompt ID and could not affect the prior managed turn. Detaching
left the pane and Claude session alive. A fresh Store/controller observed the same PID and
session UUID, reconstructed human ownership, explicitly returned ownership to the manager,
and delivered another managed prompt to that same process. Entry never invoked `--resume`.

The expired login prevented a successful API-backed `Stop`, permission-dialog experiment,
busy-input experiment, and live interruption experiment. `Stop` result/artifact parsing,
permission transitions, and interruption bookkeeping are covered with deterministic hook
tests, but remain experimental claims until repeated with valid authentication. Image
paste/drop was not practical in the automated PTY and was not tested.

## Known limits before workflow use

- Re-run the real matrix with valid authentication, including successful `Stop`, tool use,
  permission approve/deny, busy input, long input, interruption, and post-interrupt reuse.
- Determine whether the desired native-worker policy should include `local`; the present
  default is observably different from a manual Claude launch.
- Audit MCP/plugin/skill/command/hook inheritance under actual company-managed configuration.
- Define hook delivery retry/idempotency and SQLite contention behavior. Claude command hooks
  are subprocess callbacks, not a durable event transport.
- Decide how a workflow reacts if a human claims ownership while a turn is running or leaves
  unsubmitted text in the composer. tmux client count cannot prove human intent.
- Add transcript/result recovery for the narrow crash window after Claude writes its
  transcript but before the command hook commits the event. Terminal bytes must not fill that
  gap.
- Treat Claude/tmux-server loss as runtime loss. A new generation may use Claude resume during
  reconstruction, but same-process entry must never do so.
