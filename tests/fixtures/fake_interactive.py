"""Raw interactive test process with bracketed-paste support."""

from __future__ import annotations

import json
import os
import sys
import termios
import tty
from pathlib import Path

START = b"\x1b[200~"
END = b"\x1b[201~"


def record(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def main() -> int:
    log = Path(sys.argv[1])
    record(log, {"event": "started", "pid": os.getpid()})
    old = termios.tcgetattr(sys.stdin.fileno())
    tty.setraw(sys.stdin.fileno())
    os.write(sys.stdout.fileno(), b"\x1b[?2004hREADY\r\n")
    pending = bytearray()
    pasted: bytearray | None = None
    awaiting_enter = False
    try:
        while True:
            chunk = os.read(sys.stdin.fileno(), 4096)
            if not chunk:
                return 0
            pending.extend(chunk)
            while pending:
                if pasted is None:
                    if awaiting_enter:
                        if pending[:1] in (b"\r", b"\n"):
                            del pending[:1]
                            awaiting_enter = False
                            continue
                        break
                    start = pending.find(START)
                    if start >= 0:
                        del pending[: start + len(START)]
                        pasted = bytearray()
                        continue
                    newline = next(
                        (index for index, byte in enumerate(pending) if byte in (10, 13)), None
                    )
                    if newline is None:
                        break
                    text = bytes(pending[:newline]).decode("utf-8")
                    del pending[: newline + 1]
                else:
                    end = pending.find(END)
                    if end < 0:
                        pasted.extend(pending)
                        pending.clear()
                        break
                    pasted.extend(pending[:end])
                    del pending[: end + len(END)]
                    # Terminals represent pasted line breaks as carriage returns. An
                    # interactive editor normalizes those to logical newlines.
                    text = pasted.decode("utf-8").replace("\r", "\n")
                    pasted = None
                    if pending[:1] in (b"\r", b"\n"):
                        del pending[:1]
                    else:
                        awaiting_enter = True
                record(log, {"event": "turn", "text": text, "pid": os.getpid()})
                if text == "__exit__":
                    return 7
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)


if __name__ == "__main__":
    raise SystemExit(main())
