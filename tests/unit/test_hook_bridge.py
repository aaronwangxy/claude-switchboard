"""Supported Claude hook payloads drive durable native-turn semantics."""

from __future__ import annotations

from uuid import uuid4

import pytest

from switchboard.domain.contracts import extract_json_block
from switchboard.domain.enums import (
    NativeTurnOrigin,
    NativeTurnStatus,
    RuntimeAgentKind,
    RuntimeProcessState,
)
from switchboard.domain.models import NativeTurn, RuntimeInstance
from switchboard.runtime.hook_bridge import acknowledge_turn, handle_hook, managed_prompt


def runtime(store):
    return store.save_runtime(
        RuntimeInstance(
            agent_id=uuid4(),
            agent_kind=RuntimeAgentKind.WORKER,
            generation=1,
            backend="native-prototype",
            launch_fingerprint="test",
        )
    )


def test_managed_prompt_correlates_by_nonce_then_stop_captures_the_result(store):
    instance = runtime(store)
    turn = store.save_native_turn(
        NativeTurn(
            runtime_id=instance.id,
            origin=NativeTurnOrigin.MANAGED,
            correlation_token="a" * 43,
            prompt_sha256="digest",
        )
    )
    submitted = handle_hook(
        store,
        instance.id,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-1",
            "prompt_id": "prompt-1",
            "prompt": managed_prompt(turn, "produce an artifact"),
        },
    )
    active = store.get_native_turn(turn.id)

    assert submitted.turn_id == turn.id
    assert active.origin is NativeTurnOrigin.MANAGED
    assert active.status is NativeTurnStatus.ACTIVE
    assert active.claude_prompt_id == "prompt-1"
    assert store.get_runtime(instance.id).process_state is RuntimeProcessState.TURN_ACTIVE

    output = 'Done.\n```json\n{"criteria": [{"id": "AC1"}]}\n```'
    stopped = handle_hook(
        store,
        instance.id,
        {
            "hook_event_name": "Stop",
            "session_id": "session-1",
            "prompt_id": "prompt-1",
            "last_assistant_message": output,
            "stop_hook_active": False,
        },
    )
    completed = store.get_native_turn(turn.id)

    assert stopped.turn_id == turn.id
    assert completed.status is NativeTurnStatus.COMPLETED
    assert completed.final_output == output
    assert extract_json_block(completed.final_output) == {"criteria": [{"id": "AC1"}]}
    assert store.get_runtime(instance.id).process_state is RuntimeProcessState.TURN_COMPLETE

    acknowledge_turn(store, instance.id, turn.id)
    assert store.get_runtime(instance.id).process_state is RuntimeProcessState.READY


def test_human_prompt_cannot_complete_the_prior_managed_turn(store):
    instance = runtime(store)
    managed = store.save_native_turn(
        NativeTurn(
            runtime_id=instance.id,
            origin=NativeTurnOrigin.MANAGED,
            correlation_token="b" * 43,
        )
    )
    handle_hook(
        store,
        instance.id,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-1",
            "prompt_id": "managed-prompt",
            "prompt": managed_prompt(managed, "A"),
        },
    )
    handle_hook(
        store,
        instance.id,
        {
            "hook_event_name": "Stop",
            "session_id": "session-1",
            "prompt_id": "managed-prompt",
            "last_assistant_message": "A complete",
        },
    )
    handle_hook(
        store,
        instance.id,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-1",
            "prompt_id": "human-prompt",
            "prompt": "B typed directly",
        },
    )
    human = store.active_native_turn(instance.id, "human-prompt")
    handle_hook(
        store,
        instance.id,
        {
            "hook_event_name": "Stop",
            "session_id": "session-1",
            "prompt_id": "human-prompt",
            "last_assistant_message": "B complete",
        },
    )

    assert human.origin is NativeTurnOrigin.HUMAN
    assert store.get_native_turn(managed.id).final_output == "A complete"
    assert store.get_native_turn(human.id).final_output == "B complete"


def test_permission_failure_and_session_lifecycle_are_distinct(store):
    instance = runtime(store)
    handle_hook(
        store,
        instance.id,
        {"hook_event_name": "SessionStart", "session_id": "session-2", "source": "startup"},
    )
    assert store.get_runtime(instance.id).claude_session_id == "session-2"
    assert store.get_runtime(instance.id).process_state is RuntimeProcessState.READY

    handle_hook(
        store,
        instance.id,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-2",
            "prompt_id": "prompt-2",
            "prompt": "human request",
        },
    )
    handle_hook(
        store,
        instance.id,
        {
            "hook_event_name": "PermissionRequest",
            "session_id": "session-2",
            "prompt_id": "prompt-2",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
        },
    )
    waiting = store.active_native_turn(instance.id, "prompt-2")
    assert waiting.status is NativeTurnStatus.WAITING_PERMISSION
    assert store.get_runtime(instance.id).process_state is RuntimeProcessState.WAITING

    handle_hook(
        store,
        instance.id,
        {
            "hook_event_name": "StopFailure",
            "session_id": "session-2",
            "prompt_id": "prompt-2",
            "error": "server_error",
            "error_details": "unavailable",
            "last_assistant_message": "API Error",
        },
    )
    failed = store.get_native_turn(waiting.id)
    assert failed.status is NativeTurnStatus.FAILED
    assert failed.error == "server_error: unavailable"

    handle_hook(
        store,
        instance.id,
        {"hook_event_name": "SessionEnd", "session_id": "session-2", "reason": "other"},
    )
    assert store.get_runtime(instance.id).process_state is RuntimeProcessState.EXITED
    assert [event.event_name for event in store.runtime_hook_events(instance.id)] == [
        "SessionStart",
        "UserPromptSubmit",
        "PermissionRequest",
        "StopFailure",
        "SessionEnd",
    ]


def test_hook_rejects_a_different_claude_session_for_bound_runtime(store):
    instance = runtime(store)
    instance.claude_session_id = "expected-session"
    store.save_runtime(instance)

    with pytest.raises(ValueError, match="does not match"):
        handle_hook(
            store,
            instance.id,
            {
                "hook_event_name": "SessionStart",
                "session_id": "stale-session",
                "source": "startup",
            },
        )


def test_duplicate_and_stale_callbacks_do_not_corrupt_turn_ownership(store):
    instance = runtime(store)
    managed = store.save_native_turn(
        NativeTurn(
            runtime_id=instance.id,
            origin=NativeTurnOrigin.MANAGED,
            correlation_token="c" * 43,
        )
    )
    submitted = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session",
        "prompt_id": "managed-prompt",
        "prompt": managed_prompt(managed, "A"),
    }
    handle_hook(store, instance.id, submitted)
    handle_hook(store, instance.id, submitted)

    assert len(store.list_native_turns(instance.id)) == 1

    handle_hook(
        store,
        instance.id,
        {
            "hook_event_name": "PermissionRequest",
            "session_id": "session",
            "prompt_id": "stale-prompt",
        },
    )
    assert store.get_runtime(instance.id).process_state is RuntimeProcessState.TURN_ACTIVE

    handle_hook(
        store,
        instance.id,
        {
            "hook_event_name": "Stop",
            "session_id": "session",
            "prompt_id": "stale-prompt",
            "last_assistant_message": "not this turn",
        },
    )
    assert store.get_runtime(instance.id).process_state is RuntimeProcessState.TURN_ACTIVE
    assert store.get_native_turn(managed.id).final_output == ""
