"""Claude Code command-hook bridge into durable Switchboard runtime state."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from switchboard.domain.enums import NativeTurnOrigin, NativeTurnStatus, RuntimeProcessState
from switchboard.domain.models import NativeTurn, RuntimeHookEvent, now
from switchboard.storage.store import Store

MANAGED_MARKER = re.compile(
    r"(?:^|\n)<!-- switchboard-managed-turn:([0-9a-f-]{36}):([A-Za-z0-9_-]{32,}) -->\s*$"
)
WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})


def managed_prompt(turn: NativeTurn, prompt: str) -> str:
    if not turn.correlation_token:
        raise ValueError("A managed turn needs a correlation token.")
    return (
        f"{prompt}\n\n<!-- switchboard-managed-turn:{turn.id}:"
        f"{turn.correlation_token} -->"
    )


def prompt_digest(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def handle_hook(store: Store, runtime_id: UUID, payload: dict[str, Any]) -> RuntimeHookEvent:
    runtime = store.get_runtime(runtime_id)
    if runtime is None:
        raise ValueError(f"Runtime {runtime_id} does not exist.")
    name = str(payload.get("hook_event_name") or "")
    if not name:
        raise ValueError("Hook payload has no hook_event_name.")
    session_id = _string(payload.get("session_id"))
    prompt_id = _string(payload.get("prompt_id"))
    turn: NativeTurn | None = None

    if session_id:
        if runtime.claude_session_id and runtime.claude_session_id != session_id:
            raise ValueError(
                f"Claude session {session_id} does not match runtime "
                f"{runtime.claude_session_id}."
            )
        runtime.claude_session_id = session_id

    if name == "SessionStart":
        runtime.process_state = RuntimeProcessState.READY
    elif name == "UserPromptSubmit":
        prompt = str(payload.get("prompt") or "")
        turn = _correlate_prompt(store, runtime_id, prompt, prompt_id)
        turn.claude_prompt_id = prompt_id
        turn.claude_session_id = session_id
        turn.status = NativeTurnStatus.ACTIVE
        turn.updated_at = now()
        store.save_native_turn(turn)
        runtime.process_state = RuntimeProcessState.TURN_ACTIVE
    elif name == "PermissionRequest" or (
        name == "Notification"
        and payload.get("notification_type") in ("permission_prompt", "elicitation_dialog")
    ):
        turn = store.active_native_turn(runtime_id, prompt_id)
        if turn is not None:
            turn.status = NativeTurnStatus.WAITING_PERMISSION
            turn.updated_at = now()
            store.save_native_turn(turn)
            runtime.process_state = RuntimeProcessState.WAITING
    elif name in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        turn = store.active_native_turn(runtime_id, prompt_id)
    elif name in ("Stop", "StopFailure"):
        turn = store.active_native_turn(runtime_id, prompt_id)
        if turn is not None:
            turn.final_output = str(payload.get("last_assistant_message") or "")
            turn.status = (
                NativeTurnStatus.COMPLETED
                if name == "Stop"
                else NativeTurnStatus.FAILED
            )
            if name == "StopFailure":
                turn.error = ": ".join(
                    part
                    for part in (
                        _string(payload.get("error")),
                        _string(payload.get("error_details")),
                    )
                    if part
                )
            turn.updated_at = now()
            store.save_native_turn(turn)
            runtime.process_state = RuntimeProcessState.TURN_COMPLETE
    elif name == "SessionEnd":
        runtime.process_state = RuntimeProcessState.EXITED

    runtime.updated_at = now()
    store.save_runtime(runtime)
    event = RuntimeHookEvent(
        runtime_id=runtime_id,
        event_name=name,
        session_id=session_id,
        prompt_id=prompt_id,
        turn_id=turn.id if turn else None,
        payload=payload,
    )
    return store.add_runtime_hook_event(event)


def acknowledge_turn(store: Store, runtime_id: UUID, turn_id: UUID) -> NativeTurn:
    turn = store.get_native_turn(turn_id)
    if turn is None or turn.runtime_id != runtime_id:
        raise ValueError("No such native turn for this runtime.")
    if turn.status not in (
        NativeTurnStatus.COMPLETED,
        NativeTurnStatus.FAILED,
        NativeTurnStatus.INTERRUPTED,
    ):
        raise ValueError(f"Native turn is still {turn.status.value}.")
    runtime = store.get_runtime(runtime_id)
    if runtime is None:
        raise ValueError("Runtime does not exist.")
    latest = store.list_native_turns(runtime_id)
    if runtime.process_state not in (
        RuntimeProcessState.TURN_COMPLETE,
        RuntimeProcessState.WAITING,
    ):
        raise ValueError(
            f"Runtime is {runtime.process_state.value}; no completed turn is awaiting acknowledgement."
        )
    if not latest or latest[-1].id != turn_id:
        raise ValueError("A newer native turn supersedes this acknowledgement.")
    runtime.process_state = RuntimeProcessState.READY
    runtime.updated_at = now()
    store.save_runtime(runtime)
    return turn


def _correlate_prompt(
    store: Store, runtime_id: UUID, prompt: str, prompt_id: str | None
) -> NativeTurn:
    match = MANAGED_MARKER.search(prompt)
    if match:
        turn_id = UUID(match.group(1))
        token = match.group(2)
        turn = store.native_turn_by_token(runtime_id, token)
        if turn is not None and turn.id == turn_id:
            if turn.status is NativeTurnStatus.PENDING:
                return turn
            if (
                turn.status in (NativeTurnStatus.ACTIVE, NativeTurnStatus.WAITING_PERMISSION)
                and turn.claude_prompt_id == prompt_id
            ):
                # Command-hook delivery is not our transaction boundary. Treat a repeated
                # callback for the same Claude prompt as idempotent, never as human input.
                return turn
    return NativeTurn(
        runtime_id=runtime_id,
        origin=NativeTurnOrigin.HUMAN,
        status=NativeTurnStatus.PENDING,
        prompt_sha256=prompt_digest(prompt),
    )


def _string(value: Any) -> str | None:
    return str(value) if value is not None else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--runtime-id", type=UUID, required=True)
    parser.add_argument("--deny-write-tools", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Hook input must be a JSON object.")
        store = Store(args.database)
        try:
            handle_hook(store, args.runtime_id, payload)
        finally:
            store.close()
        if (
            args.deny_write_tools
            and payload.get("hook_event_name") == "PreToolUse"
            and payload.get("tool_name") in WRITE_TOOLS
        ):
            print("Switchboard read-only worker: file-editing tool denied.", file=sys.stderr)
            return 2
    except Exception as exc:
        # Nonzero but not 2: observability must never block Claude or override policy.
        print(f"Switchboard hook bridge failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
