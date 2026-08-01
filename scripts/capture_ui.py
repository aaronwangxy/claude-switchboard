"""Regenerate the terminal captures in `docs/ui-*.txt`.

The captures are evidence, so they have to be reproducible rather than pasted. This
drives the real application headlessly on the scripted backend against a throwaway
`SB_HOME` and a throwaway repository, so no real state is touched and no model is
called.

    ./.venv/bin/python scripts/capture_ui.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"

TICKET = """ENG-421 Notification preferences

Users need per-channel notification preferences that persist across restarts.
The dispatcher must honour them for every outbound channel.
Acceptance: preferences survive a restart and the dispatcher reads them.
"""

SECOND_TICKET = """ENG-999 Rewrite the billing exporter

The nightly billing exporter times out on large tenants and needs a streaming
rewrite so memory stays flat. Acceptance: exports finish under ten minutes.
"""


def make_repo(root: Path) -> Path:
    path = root / "repo-alpha"
    path.mkdir(parents=True)

    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)

    run("init", "-b", "main", "--quiet")
    run("config", "user.email", "csm@example.com")
    run("config", "user.name", "CSM")
    run("config", "commit.gpgsign", "false")
    (path / "README.md").write_text("# alpha\n")
    run("add", "README.md")
    run("commit", "--quiet", "-m", "initial commit")
    return path


async def settle(pilot, ticks: int = 12) -> None:
    for _ in range(ticks):
        await pilot.pause()
        await asyncio.sleep(0.02)


def screen_text(app) -> str:
    """The rendered screen as plain text, right-trimmed line by line.

    Textual exports SVG, not text, so this reaches for the compositor. Private API: if a
    Textual upgrade breaks it, the fix is here and nothing in `switchboard` is affected.
    """
    strips = app.screen._compositor.render_strips()
    return "\n".join(strip.text.rstrip() for strip in strips).rstrip()


def write(name: str, title: str, screen: str) -> None:
    path = DOCS / name
    path.write_text(f"{title}\n{'=' * len(title)}\n\n{screen}\n")
    print(f"wrote {path.relative_to(DOCS.parent)}")


async def capture(repo: Path) -> None:
    from switchboard.app import build_app

    app = build_app(register=[repo])
    async with app.run_test(size=(150, 40)) as pilot:
        await settle(pilot)
        write("ui-01-startup.txt", "sb — startup, no workers yet", screen_text(pilot.app))

        await pilot.app._manager_turn(TICKET)
        await settle(pilot)
        workers = pilot.app.sm.store.list_workers()
        if workers:
            pilot.app.select_worker(workers[0].id)
        await settle(pilot)
        write(
            "ui-02-blocked-planner.txt",
            "sb — blocked planner with attention banner",
            screen_text(pilot.app),
        )

        await pilot.app._manager_turn(SECOND_TICKET)
        await settle(pilot)
        write("ui-03-two-workers.txt", "sb — two jobs in flight", screen_text(pilot.app))

        pilot.app.query_one("#worker-table").focus()
        await pilot.press("question_mark")
        await settle(pilot, ticks=4)
        write("ui-04-help.txt", "sb — help screen", screen_text(pilot.app))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="csm-capture-") as tmp:
        root = Path(tmp)
        os.environ["SB_HOME"] = str(root / "home")
        os.environ["SB_WORKFLOWS_DIR"] = str(root / "workflows")
        os.environ["SB_BACKEND"] = "scripted"
        asyncio.run(capture(make_repo(root)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
