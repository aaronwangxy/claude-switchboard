#!/usr/bin/env python3
"""Claude-shaped interactive process that emits real hook subprocess callbacks."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path
from uuid import uuid4

START = b"\x1b[200~"
END = b"\x1b[201~"


def option(name: str) -> str:
    index = sys.argv.index(name)
    return sys.argv[index + 1]


def hook_command() -> list[str]:
    settings = json.loads(Path(option("--settings")).read_text())
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    return shlex.split(command)


def emit(command: list[str], session_id: str, name: str, **payload) -> None:
    body = {
        "session_id": session_id,
        "transcript_path": str(Path.cwd() / "fake-transcript.jsonl"),
        "cwd": str(Path.cwd()),
        "hook_event_name": name,
        **payload,
    }
    subprocess.run(command, input=json.dumps(body), text=True, check=True)


def record(payload: dict) -> None:
    path = os.environ.get("FAKE_NATIVE_LOG")
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")


def response_for(prompt: str) -> str:
    if os.environ.get("FAKE_NATIVE_COMPOSITE") != "1":
        return os.environ.get("FAKE_NATIVE_RESPONSE", "Native result")
    if "Implement the approved plan" in prompt:
        Path("native-change.txt").write_text("implemented\n")
        subprocess.run(["git", "add", "native-change.txt"], check=True)
        subprocess.run(
            ["git", "commit", "-m", "feat: native composite change"],
            check=True,
            capture_output=True,
        )
        return "Implemented and committed the approved change."
    if "Verify this change" in prompt:
        return (
            "Verified.\n```json\n"
            '{"scope":"full","evidence":[{"criterion_id":"AC1","status":"passed",'
            '"commands":[{"command":"test -f native-change.txt","exit_code":0,'
            '"output_excerpt":"present"}],"observed_behavior":"change exists",'
            '"artifacts":["native-change.txt"],"limitations":[]}]}\n```'
        )
    if "Review this change independently" in prompt:
        return 'Review passed.\n```json\n{"verdict":"pass","findings":[]}\n```'
    return os.environ.get("FAKE_NATIVE_RESPONSE", "Native result")


def main() -> int:
    session_id = option("--session-id")
    command = hook_command()
    emit(command, session_id, "SessionStart", source="startup", model="fake")
    record({"event": "started", "pid": os.getpid(), "argv": sys.argv[1:]})
    old = termios.tcgetattr(sys.stdin.fileno())
    tty.setraw(sys.stdin.fileno())
    os.write(sys.stdout.fileno(), b"\x1b[?2004hFAKE CLAUDE READY\r\n")
    pending = bytearray()
    pasted: bytearray | None = None
    try:
        while True:
            pending.extend(os.read(sys.stdin.fileno(), 4096))
            start = pending.find(START)
            if pasted is None and start >= 0:
                del pending[: start + len(START)]
                pasted = bytearray()
            if pasted is None:
                continue
            end = pending.find(END)
            if end < 0:
                pasted.extend(pending)
                pending.clear()
                continue
            pasted.extend(pending[:end])
            del pending[: end + len(END)]
            if pending[:1] in (b"\r", b"\n"):
                del pending[:1]
            prompt = pasted.decode("utf-8").replace("\r", "\n")
            pasted = None
            prompt_id = str(uuid4())
            record({"event": "prompt", "pid": os.getpid(), "text": prompt})
            emit(command, session_id, "UserPromptSubmit", prompt_id=prompt_id, prompt=prompt)
            if "NOTIFICATION_PERMISSION" in prompt:
                emit(
                    command,
                    session_id,
                    "Notification",
                    prompt_id=prompt_id,
                    notification_type="permission_prompt",
                    message="Permission needed",
                )
            elif "PERMISSION_TEST" in prompt:
                emit(
                    command,
                    session_id,
                    "PermissionRequest",
                    prompt_id=prompt_id,
                    tool_name="Bash",
                    tool_input={"command": "pwd"},
                )
            if "HOLD_TURN" in prompt:
                time.sleep(0.5)
            response = response_for(prompt)
            if "STOP_FAILURE" in prompt:
                emit(
                    command,
                    session_id,
                    "StopFailure",
                    prompt_id=prompt_id,
                    error="fake_failure",
                    last_assistant_message="[NEEDS INPUT] failed output must not harvest",
                )
            else:
                emit(
                    command,
                    session_id,
                    "Stop",
                    prompt_id=prompt_id,
                    last_assistant_message=response,
                    stop_hook_active=False,
                )
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)


if __name__ == "__main__":
    raise SystemExit(main())
