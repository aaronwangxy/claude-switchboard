# The runtime substrate

Every Switchboard session — manager and workers alike — is a real native Claude Code
process running in tmux. The board is a client of those processes, not their owner: quit
`sb` and they keep running; start it again and it adopts them.

```
SessionManager
  → NativeClaudeBackend
  → NativeClaudeRuntime / TmuxRuntimeSupervisor
  → a generation-bound tmux pane
  → native Claude Code
  → Claude lifecycle hooks
  → a durable native turn + a normalised WorkerEvent
  → contracts, evidence, freshness, workflow completion
```

`SB_BACKEND=scripted` swaps in `ScriptedWorkerBackend`, which emits the same normalised
events in-process for deterministic tests and offline demos. There is no SDK fallback;
`claude-agent-sdk` is not a dependency.

## Runtime instances

Each agent — worker or manager — has a durable, generation-numbered `RuntimeInstance`. It
records substrate-neutral process and turn state, whether the manager or a human owns the
input lane, the Claude session UUID, the launch fingerprint, opaque substrate identity,
and the Git baseline for an active writable turn.

The launch fingerprint hashes the executable, cwd, model, worker prompt, environment
additions, hook interpreter/event set/database, and state directory. It is what decides
whether a live process may be adopted, so it is a hash input rather than a name: the
adapter string inside it is frozen deliberately.

## tmux topology

One dedicated Switchboard tmux server and socket, with **one tmux session per runtime
generation** — not one shared session with many windows. Separate sessions give each
process an independent attach target, client count, ownership metadata, exit state, and
cleanup boundary, and they avoid changing every attached user's window when another
runtime is selected. A shared server still permits `switch-client`.

`TmuxController` contains every tmux command and parser; `TmuxRuntimeSupervisor` binds
exact targets to `RuntimeInstance`.

The durable binding has two halves:

- `RuntimeInstance.substrate` stores the tmux session name, pane id, and pane PID as
  opaque data;
- tmux user options store the runtime UUID, generation, launch fingerprint, and controller
  owner on the session itself.

Observation accepts a runtime only when **both** halves match. A reused name, a changed
generation or fingerprint, or a different pane identity is stale rather than adoptable. If
the tmux half is exact but Python died before saving the opaque target, recovery repairs
the durable half and adopts it. Concurrent creators are serialised by tmux's session-name
creation: the loser waits briefly for the winner's metadata and adopts the same pane
rather than launching a second child.

This layer observes process lifetime and ownership only. It never interprets terminal
contents as Claude readiness, turn completion, blocking, or permission state — a newly
launched live pane stays `STARTING` until a hook says otherwise.

## Input

Programmatic input uses `tmux load-buffer` with the prompt on stdin, followed by a
bracketed `paste-buffer -p` and a separate Enter key. Prompt bytes are never command
arguments and no shell interpolation is involved. Ownership changes and the
load/paste/Enter transaction share a tmux-native per-runtime lock, so two controller
processes cannot interleave two turns.

Programmatic input requires manager ownership **and** zero attached tmux clients. An
attached client is a conservative extra lock; attaching alone does not durably claim
ownership, so a caller must claim it before presenting the view.

## Hooks, turns, and provenance

Claude command hooks — not pane contents — are the lifecycle evidence. Switchboard writes
a mode-0600 settings overlay registering hooks for `SessionStart`, `UserPromptSubmit`,
tool events, `PermissionRequest`, `Notification`, `Stop`, `StopFailure`,
`InstructionsLoaded`, and `SessionEnd`. The overlay *adds* Switchboard's hooks; it never
replaces the user's settings and never chooses a bypass permission mode.

Before injecting a prompt, Switchboard durably creates a `PENDING` turn with a random
256-bit token and appends an HTML comment carrying the turn UUID and that token.
`UserPromptSubmit` validates both and binds Claude's `prompt_id` to the turn. Later hooks
use that prompt id, so a human prompt typed into the same session cannot complete an
earlier managed turn. A prompt without a valid pending marker becomes a `HUMAN` turn.

**Only a turn whose durable origin is `MANAGED`, and which no human touched, becomes a
`WorkerEvent`.** Human turns and their `Stop` events are still recorded, but they cannot
harvest artifacts or advance a workflow.

`Stop.last_assistant_message` is the turn's result. It flows through the same
`_finish_turn` and fenced-JSON contract parsers the scripted tests use. The hook delivery
ledger is checked and marked by `SessionManager` using the hook event UUID, so a replay
after a controller restart cannot duplicate a transcript row, an artifact, or a run
advancement.

Interruption is deliberately conservative. Claude documents that a user interruption emits
no `Stop`, so a Switchboard-issued Ctrl-C records `INTERRUPT_REQUESTED` and keeps the
input lane closed until a supported lifecycle event or explicit recovery resolves it.
Delivering the key is not evidence that Claude stopped.

## Entering a session, and handing it back

Entry is a view description, not a child process Switchboard owns. The board claims human
ownership, then attaches the terminal to the existing tmux target:

```bash
tmux -S <switchboard socket> attach-session -t <runtime session>
```

Entry never invokes `claude --resume`, never replaces the process, and never interrupts an
active turn. A client already connected to the same dedicated server uses `switch-client`;
a client belonging to *another* tmux server is told to open a separate terminal rather than
nesting tmux. Detaching leaves the pane process alive.

While a human owns a runtime, Switchboard refuses to send to it, and any composite run the
worker belongs to is paused and marked human-intervened. Handing control back requires an
explicit confirmation that Claude's composer is empty — tmux cannot prove composer state.
The run stays paused until you say so: you may have edited by hand, and whether the ritual
should carry on is your decision, not the application's.

Entering while a managed turn is active durably taints that turn, so its result cannot
advance a workflow. That is the safety property, and it is also
[the sharpest edge in the product today](troubleshooting.md#known-limitations).

## Recovery

`recover()` observes the backend before it trusts durable state.

| What it observes | What it does |
| --- | --- |
| Exact runtime id, generation, and fingerprint alive | Adopt it, same PID and Claude session |
| A live process whose identity or generation does not match | Refuse: mark the worker disconnected rather than adopt or replace |
| No process, manager-owned | Reconstruct as a **new generation** and a fresh Claude session, seeded from durable Switchboard state |
| No process, human-owned | Refuse: ownership must be returned explicitly first |
| Worktree missing | Mark disconnected with a recovery instruction |

Reconstruction is not ordinary entry, and it never makes Claude's own history
authoritative — the new session is seeded from Switchboard's stored contracts.

After workers are reconciled, each running composite run is checked. A run whose step
runtime vanished before a trusted completion is blocked rather than resent. A run whose
prompt delivery is uncertain (a `PENDING` turn on a `READY` runtime) is blocked with
instructions. Only a run with no current worker, or one whose step is durably complete,
advances.

## Read-only workers

Read-only roles launch in native `plan` permission mode, so Claude withholds editing
operations while managed and company policy still applies, and the hook bridge denies
`Edit`/`Write`/`NotebookEdit`/`MultiEdit` outright. They keep `Bash`, because reviewers
and verifiers need `git log`, `git diff`, and test commands — which is why read-only is a
tool policy rather than a sandbox.
