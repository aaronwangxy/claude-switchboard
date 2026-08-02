#!/usr/bin/env bash

set -u

# Always operate from the repository root.
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

# Quickly start another fresh shift after a successful one.
SUCCESS_DELAY=10

# If Claude fails (including usage/rate limiting), don't hammer it.
FAILURE_DELAY=300

PROMPT='
Run exactly one autonomous Switchboard dogfood shift.

Read .claude/loop.md and follow it completely.
Treat docs/dogfood/STATE.md as the authoritative handover from previous shifts.

You are a fresh Claude session with no memory of previous shifts beyond repository
state. Reconstruct whatever context you need from the repository, STATE.md,
Switchboard runtime state, and git history.

Do one coherent shift, follow the closeout procedure in .claude/loop.md, then exit.
'

# Prevent accidentally running two supervisors.
LOCK_DIR="/tmp/switchboard-dogfood-loop.lock"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "A Switchboard dogfood supervisor appears to already be running."
    echo "If it is not, remove $LOCK_DIR and try again."
    exit 1
fi

CHILD_PID=""

cleanup() {
    echo
    echo "Stopping Switchboard dogfood supervisor..."

    if [[ -n "$CHILD_PID" ]] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill -TERM "$CHILD_PID" 2>/dev/null
        wait "$CHILD_PID" 2>/dev/null
    fi

    rmdir "$LOCK_DIR" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

while true; do
    echo
    echo "============================================================"
    echo "Starting fresh Switchboard dogfood shift: $(date)"
    echo "============================================================"

    claude -p \
        --no-session-persistence \
        --permission-mode auto \
        "$PROMPT" &

    CHILD_PID=$!
    wait "$CHILD_PID"
    STATUS=$?
    CHILD_PID=""

    if [[ "$STATUS" -eq 0 ]]; then
        echo
        echo "Dogfood shift completed successfully."
        echo "Starting another fresh shift in ${SUCCESS_DELAY}s..."
        sleep "$SUCCESS_DELAY"
    else
        echo
        echo "Claude exited with status $STATUS."
        echo "Possibly rate/usage limited; retrying in ${FAILURE_DELAY}s..."
        sleep "$FAILURE_DELAY"
    fi
done