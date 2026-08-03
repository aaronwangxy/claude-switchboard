"""What a blocked worker's permission prompt says it is waiting for.

The prompt itself only exists inside the worker's pane. Its hook payload carries the
tool input, so the one thing the board and the Manager can be told without entering the
session is *which* call is waiting -- "Permission required for Bash." names a tool that
prompts dozens of times a job and identifies none of them.
"""

from __future__ import annotations

from switchboard.agents.native_backend import PERMISSION_DETAIL_MAX, permission_summary


def test_a_bash_prompt_names_the_command_it_is_waiting_on():
    summary = permission_summary(
        {"tool_name": "Bash", "tool_input": {"command": "./.venv/bin/python -m pytest -q"}}
    )
    assert summary == "Permission required for Bash: ./.venv/bin/python -m pytest -q"


def test_a_multi_line_command_is_collapsed_and_bounded():
    command = "git commit -m \"$(cat <<'EOF'\n" + "long line " * 40 + "\nEOF\n)\""
    summary = permission_summary({"tool_name": "Bash", "tool_input": {"command": command}})

    assert "\n" not in summary
    assert len(summary) <= len("Permission required for Bash: ") + PERMISSION_DETAIL_MAX
    assert summary.startswith('Permission required for Bash: git commit -m "$(cat <<\'EOF\'')
    assert summary.endswith("...")


def test_a_tool_without_a_command_is_identified_by_its_path():
    summary = permission_summary({"tool_name": "Read", "tool_input": {"file_path": "README.md"}})
    assert summary == "Permission required for Read: README.md"


def test_a_prompt_with_nothing_identifying_reads_as_it_always_did():
    assert permission_summary({"tool_name": "AskUserQuestion", "tool_input": {"questions": []}}) == (
        "Permission required for AskUserQuestion."
    )
    assert permission_summary({}) == "Permission required for tool."
