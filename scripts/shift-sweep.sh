#!/usr/bin/env bash
#
# Sweep the leftovers of an autonomous dogfood shift.
#
# A shift that crashes, is killed, or hits a usage limit never reaches the closeout in
# `.claude/loop.md`. This script is the part that still runs: the supervisor invokes it
# before and after every shift and from its exit trap, so recovery does not depend on the
# shift having behaved.
#
#   shift-sweep.sh            report only; exit 1 if anything needs a human
#   shift-sweep.sh --clean    additionally tear down this harness's own leftovers
#
# What --clean may remove is deliberately narrow: things a shift created and named as its
# own. It never touches your personal board, never kills a session it cannot prove is an
# experiment, and never discards uncommitted work.

set -u

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

CLEAN=0
[[ "${1:-}" == "--clean" ]] && CLEAN=1

# A running shift's board, workers and test fixtures look exactly like leftovers. They are
# not, so cleaning during a shift would tear down the work in progress. The supervisor only
# calls --clean between shifts; a person running it by hand gets downgraded to a report.
if [[ "$CLEAN" == "1" ]] && pgrep -f 'autonomous Switchboard dogfood shift' >/dev/null 2>&1; then
    echo "A dogfood shift is running; reporting only. Stop the shift first to clean."
    CLEAN=0
fi

# Your personal board. Reported on, never cleaned.
PERSONAL_HOME="${SB_PERSONAL_HOME:-$HOME/.local/share/switchboard}"

# A shift names every tmux session it creates with this prefix, so a sweep can tell an
# experiment from your own work without guessing. See `.claude/loop.md`.
EXPERIMENT_PREFIX="sbx-"

problems=0
note() { printf '  %s\n' "$*"; }
section() { printf '\n%s\n' "$*"; }
needs_human() { problems=$((problems + 1)); printf '  NEEDS YOU: %s\n' "$*"; }

# --------------------------------------------------------------------------------------
section "Working tree"

if [[ -n "$(git status --porcelain)" ]]; then
    # Never discarded, never auto-committed: a crashed shift's diff is evidence, and the
    # next shift is required to adopt or commit it before starting anything new.
    needs_human "uncommitted changes — adopt or commit them before new work:"
    git status --short | sed 's/^/    /'
else
    note "clean"
fi

unpushed="$(git log --oneline @{u}.. 2>/dev/null | wc -l | tr -d ' ')"
if [[ "$unpushed" != "0" && -n "$unpushed" ]]; then
    note "$unpushed commit(s) not pushed to origin"
fi

# --------------------------------------------------------------------------------------
section "Experiment tmux sessions (prefix ${EXPERIMENT_PREFIX})"

# Sessions the harness created on the default socket. Safe to remove: the naming
# convention is what makes them identifiable as ours.
experiment_sessions="$(tmux ls -F '#{session_name}' 2>/dev/null | grep "^${EXPERIMENT_PREFIX}" || true)"
if [[ -z "$experiment_sessions" ]]; then
    note "none"
else
    while read -r s; do
        [[ -z "$s" ]] && continue
        if [[ "$CLEAN" == "1" ]]; then
            tmux kill-session -t "$s" 2>/dev/null && note "killed $s"
        else
            note "$s (still up)"
        fi
    done <<<"$experiment_sessions"
fi

# Sessions on the default socket that look like a board but are not named as an
# experiment. Reported, never killed — one of them may be yours.
stray="$(tmux ls -F '#{session_name}' 2>/dev/null | grep -vE "^${EXPERIMENT_PREFIX}" || true)"
if [[ -n "$stray" ]]; then
    while read -r s; do
        [[ -z "$s" ]] && continue
        needs_human "unnamed tmux session '$s' — yours, or a shift that ignored the naming rule?"
    done <<<"$stray"
fi

# --------------------------------------------------------------------------------------
section "Throwaway Switchboard runtimes"

# A worker/manager runtime is this harness's to clean only when its SB_HOME is a
# throwaway one -- under /tmp or a scratchpad. Anything under the personal home is
# yours and is only ever reported.
while read -r pid settings; do
    [[ -z "$pid" ]] && continue
    if [[ "$settings" == "$PERSONAL_HOME"* ]]; then
        continue
    fi
    if [[ "$CLEAN" == "1" ]]; then
        kill "$pid" 2>/dev/null && note "killed throwaway runtime $pid"
    else
        note "throwaway runtime $pid still running"
    fi
done < <(ps -eo pid,command 2>/dev/null \
    | grep -E -- '--settings [^ ]*/runtime/hooks/native-[^ ]*\.settings\.json' \
    | grep -v grep \
    | grep -v 'fake_native_claude.py' \
    | sed -E 's/^ *([0-9]+).*--settings ([^ ]+).*/\1 \2/')

# Test-tier leftovers: a native test that died without killing its fixture.
while read -r pid _; do
    [[ -z "$pid" ]] && continue
    if [[ "$CLEAN" == "1" ]]; then
        kill "$pid" 2>/dev/null && note "killed test fixture $pid"
    else
        note "test fixture $pid still running"
    fi
done < <(pgrep -f 'tests/fixtures/fake_native_claude.py' 2>/dev/null | sed 's/$/ x/')

# --------------------------------------------------------------------------------------
section "Your personal board ($PERSONAL_HOME)"

# Never cleaned. A shift is allowed to use the personal board, but it must not leave it
# in a state you cannot pick up -- so the sweep reports its health and the shift is
# responsible for resolving what it broke.
DB="$PERSONAL_HOME/switchboard.db"
if [[ ! -f "$DB" ]]; then
    note "no database yet"
else
    q() { sqlite3 "$DB" "$1" 2>/dev/null; }

    blocked="$(q "select count(*) from workflow_runs where status='blocked'")"
    [[ "${blocked:-0}" != "0" ]] && needs_human "$blocked blocked workflow run(s) — resume or retire them"

    stuck="$(q "select count(*) from workers where status in ('disconnected','error')")"
    [[ "${stuck:-0}" != "0" ]] && needs_human "$stuck worker(s) disconnected or errored"

    pending="$(q "select count(*) from attention_items where handled=0")"
    [[ "${pending:-0}" != "0" ]] && note "$pending unhandled attention item(s)"

    # A live session with no board in front of it is exactly the state that makes the
    # personal board feel broken: reachable, but nothing is driving it.
    sock="$PERSONAL_HOME/runtime/tmux.sock"
    if [[ -S "$sock" ]]; then
        sessions="$(tmux -S "$sock" ls 2>/dev/null | wc -l | tr -d ' ')"
        if [[ "${sessions:-0}" != "0" ]] && ! pgrep -f 'bin/sb' >/dev/null 2>&1; then
            needs_human "$sessions native session(s) alive with no board running — start \`sb\` to adopt, or retire them"
        fi
    fi
fi

# --------------------------------------------------------------------------------------
section "Worktrees"

git worktree list --porcelain 2>/dev/null | grep '^worktree ' | sed 's/^worktree //' | while read -r wt; do
    [[ "$wt" == "$ROOT" ]] && continue
    if [[ ! -d "$wt" ]]; then
        needs_human "worktree registered but missing: $wt (run: git worktree prune)"
    elif [[ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]]; then
        needs_human "worktree has uncommitted work, left alone: $wt"
    else
        note "clean: $wt"
    fi
done

# --------------------------------------------------------------------------------------
section "Handover"

STATE="docs/dogfood/STATE.md"
if [[ ! -f "$STATE" ]]; then
    needs_human "$STATE is missing — the next shift has no handover"
else
    age_days=$(( ( $(date +%s) - $(stat -f %m "$STATE" 2>/dev/null || echo 0) ) / 86400 ))
    note "$STATE updated ${age_days}d ago"
fi

printf '\n'
if [[ "$problems" -gt 0 ]]; then
    printf 'Sweep: %d item(s) need a human.\n' "$problems"
    exit 1
fi
printf 'Sweep: clean.\n'
exit 0
