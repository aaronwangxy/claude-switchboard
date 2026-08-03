"""Which native hooks describe a permission prompt the user is already looking at.

Claude Code emits two hooks for one prompt: a `PermissionRequest`, then a `Notification`
restating the same unanswered prompt several seconds later without a tool name. Counting
both doubles every attention number on the board. Suppressing too eagerly is the worse
failure: a prompt nobody is told about is a session that stalls in silence.
"""

from __future__ import annotations

from uuid import uuid4

from switchboard.agents.native_backend import restates_an_open_prompt
from switchboard.domain.models import RuntimeHookEvent

RUNTIME = uuid4()


def hook(name: str, **payload) -> RuntimeHookEvent:
    return RuntimeHookEvent(runtime_id=RUNTIME, event_name=name, payload=payload)


def permission_prompt() -> RuntimeHookEvent:
    return hook("Notification", notification_type="permission_prompt")


def test_the_notification_trailing_a_permission_request_is_a_restatement():
    request = hook("PermissionRequest", tool_name="Bash")
    trailing = permission_prompt()

    assert not restates_an_open_prompt([request], request)
    assert restates_an_open_prompt([request, trailing], trailing)


def test_a_notification_after_the_tool_ran_is_a_new_prompt():
    events = [
        hook("PermissionRequest", tool_name="Bash"),
        permission_prompt(),
        hook("PostToolUse", tool_name="Bash"),
        hook("PermissionRequest", tool_name="Bash"),
        permission_prompt(),
    ]

    assert restates_an_open_prompt(events, events[-1])


def test_pre_tool_use_does_not_separate_two_prompts():
    """Claude fires PreToolUse just *before* the prompt for that same tool."""
    events = [
        hook("PreToolUse", tool_name="Bash"),
        hook("PermissionRequest", tool_name="Bash"),
        permission_prompt(),
    ]

    assert restates_an_open_prompt(events, events[-1])


def test_a_permission_request_is_never_suppressed():
    """A refused prompt can be followed by another with no tool run in between."""
    events = [
        hook("PermissionRequest", tool_name="Bash"),
        permission_prompt(),
        hook("PermissionRequest", tool_name="Write"),
    ]

    assert not restates_an_open_prompt(events, events[-1])


def test_a_notification_that_restates_nothing_still_reaches_the_user():
    """Some dialogs arrive as a Notification alone; that is the only news about them."""
    events = [hook("UserPromptSubmit"), permission_prompt()]

    assert not restates_an_open_prompt(events, events[-1])
