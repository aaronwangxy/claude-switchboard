# Tmux runtime substrate

Phase 2 chooses one dedicated Switchboard tmux server/socket with one tmux session per
runtime generation. It does not put every runtime in windows of one shared session.

Separate runtime sessions give each process an independent attach target, client count,
ownership metadata, exit state, and cleanup boundary. They also avoid changing every
attached user's window when another runtime is selected. A shared server still permits
`switch-client` when the board is eventually hosted by that same server.

The durable binding has two halves:

- `RuntimeInstance.substrate` stores tmux session name, pane ID, and pane PID as opaque data.
- Tmux user options store the runtime UUID, generation, launch fingerprint, and controller
  owner on the session itself.

Observation accepts a runtime only when both halves match. A reused name, changed generation
or fingerprint, or different pane identity is stale rather than adoptable.
If the tmux half is exact but Python died before saving the opaque target, recovery repairs the
durable half and adopts it. Concurrent creators are serialized by tmux's session-name creation;
the loser waits briefly for the winner's metadata and adopts the same pane rather than launching
a second child.

Input uses `tmux load-buffer` with the prompt on stdin, followed by bracketed
`paste-buffer -p` and a separate Enter key. Prompt bytes are never command arguments and no
shell interpolation is used. Interactive applications normalize terminal carriage returns
inside a bracketed paste back to logical newlines.
Ownership changes and the load/paste/Enter transaction share a tmux-native per-runtime lock,
so separate controller processes cannot interleave two turns.

Entry is a view description, not a blocking child process owned by Switchboard. An external
terminal attaches to the existing session. A client already connected to the same dedicated
server uses `switch-client`; a client belonging to another tmux server is told to open a
separate terminal instead of nesting tmux. Detaching a client leaves the pane process alive.

Ownership is explicit rather than inferred from screen bytes. Switchboard writes
`manager`/`human` to durable state and a tmux user option. The option reconstructs ownership
after a Python restart. Programmatic input requires manager ownership and zero attached tmux
clients. An attached client is therefore a conservative additional lock, but attaching alone
does not durably claim ownership; the caller must claim it before presenting the view.

This layer observes only process lifetime and ownership. It does not interpret terminal
contents as Claude readiness, turn completion, blocking, or permission state.
A newly launched live pane therefore remains `STARTING`; tmux does not promote it to semantic
`READY`.
