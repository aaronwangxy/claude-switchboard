"""Native manager launch isolation is structural rather than prompt-only."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from switchboard.agents.manager_mcp import TOOL_SCHEMAS
from switchboard.agents.native_backend import NativeClaudeBackend
from switchboard.agents.native_manager import PersistentNativeManager
from switchboard.config import Config


def fake_backend(tmp_path: Path):
    backend = object.__new__(NativeClaudeBackend)
    backend.controller = SimpleNamespace(socket_path=tmp_path / "runtime" / "tmux.sock")
    return backend


def test_manager_has_dedicated_non_repository_workspace(session_manager, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    backend = fake_backend(tmp_path)
    manager = PersistentNativeManager(session_manager, backend, state)  # type: ignore[arg-type]
    assert manager.workspace == state / "manager-workspace"
    assert not (manager.workspace / ".git").exists()
    assert manager.workspace != Path.cwd()


def test_manager_mcp_config_is_generation_bound(session_manager, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    backend = fake_backend(tmp_path)
    manager = PersistentNativeManager(session_manager, backend, state)  # type: ignore[arg-type]
    from switchboard.domain.enums import RuntimeAgentKind
    from switchboard.domain.models import RuntimeInstance

    runtime = RuntimeInstance(
        agent_id=manager.manager_id, agent_kind=RuntimeAgentKind.MANAGER, backend="native"
    )
    session_manager.store.save_runtime(runtime)
    manager._write_mcp_config(runtime)
    config = json.loads(manager._mcp_path(runtime).read_text())
    command = config["mcpServers"]["switchboard"]
    joined = " ".join(command["args"])
    assert str(runtime.id) in joined
    assert command["command"].endswith("python") or "python" in command["command"]
    assert set(TOOL_SCHEMAS)


async def test_manager_mcp_socket_routes_to_board_session_manager(session_manager, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    manager = PersistentNativeManager(session_manager, fake_backend(tmp_path), state)  # type: ignore[arg-type]
    from switchboard.domain.enums import RuntimeAgentKind
    from switchboard.domain.models import RuntimeInstance

    runtime = RuntimeInstance(
        agent_id=manager.manager_id, agent_kind=RuntimeAgentKind.MANAGER, backend="native"
    )
    session_manager.store.save_runtime(runtime)
    try:
        await manager._ensure_mcp_server(runtime)
    except PermissionError:
        import pytest

        pytest.skip("sandbox forbids local Unix sockets")
    import asyncio

    reader, writer = await asyncio.open_unix_connection(manager._socket_path(runtime))
    writer.write(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"status_summary","arguments":{}}}\n')
    await writer.drain()
    response = json.loads(await reader.readline())
    assert response["id"] == 1 and "content" in response["result"]
    writer.close()
    await writer.wait_closed()


def test_manager_launch_disables_coding_tools(session_manager, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    backend = fake_backend(tmp_path)
    manager = PersistentNativeManager(session_manager, backend, state)  # type: ignore[arg-type]
    from switchboard.domain.enums import RuntimeAgentKind
    from switchboard.domain.models import RuntimeInstance

    runtime = RuntimeInstance(
        agent_id=manager.manager_id, agent_kind=RuntimeAgentKind.MANAGER, backend="native"
    )
    args = manager._extra_args(runtime)
    assert "--strict-mcp-config" in args
    assert "--tools" in args and args[args.index("--tools") + 1] == ""
    denied = args[args.index("--disallowedTools") + 1]
    assert all(name in denied for name in ("Bash", "Edit", "Write", "Read", "Task"))
    assert "mcp__switchboard__*" in args


#: A stand-in for the board's socket server whose death is a real process exit, which is
#: how a controller actually goes away.
BOARD_STUB = """
import asyncio, json, sys
from pathlib import Path

path = Path(sys.argv[1])


async def handle(reader, writer):
    while line := await reader.readline():
        request = json.loads(line)
        reply = {"jsonrpc": "2.0", "id": request.get("id"),
                 "result": {"content": [{"type": "text", "text": "board"}]}}
        writer.write((json.dumps(reply) + "\\n").encode())
        await writer.drain()
    writer.close()


async def main():
    path.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(handle, path)
    async with server:
        await server.serve_forever()


asyncio.run(main())
"""


async def test_manager_mcp_bridge_survives_a_controller_restart(tmp_path):
    """Quitting the board must not permanently strip a live Manager of its tools.

    Claude Code never respawns a stdio MCP server that exits, so a bridge that died with
    the board left the surviving Manager narrating tool calls it could no longer make.
    """
    import asyncio
    import sys

    socket_path = Path("/private/tmp") / f"sb-bridge-test-{id(tmp_path)}.sock"
    socket_path.unlink(missing_ok=True)

    async def start_board():
        board = await asyncio.create_subprocess_exec(
            sys.executable, "-c", BOARD_STUB, str(socket_path)
        )
        for _ in range(100):
            if socket_path.exists():
                return board
            await asyncio.sleep(0.05)
        raise AssertionError("board stub never bound its socket")

    try:
        board = await start_board()
    except PermissionError:  # pragma: no cover - sandbox only
        import pytest

        pytest.skip("sandbox forbids local Unix sockets")

    proxy = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "switchboard.agents.manager_mcp",
        "--socket",
        str(socket_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    assert proxy.stdin is not None and proxy.stdout is not None

    async def call(ident: int) -> dict:
        request = {"jsonrpc": "2.0", "id": ident, "method": "tools/list"}
        proxy.stdin.write((json.dumps(request) + "\n").encode())
        await proxy.stdin.drain()
        return json.loads(await asyncio.wait_for(proxy.stdout.readline(), timeout=30))

    board2 = None
    try:
        assert (await call(1))["id"] == 1

        board.kill()  # the controller quits; the socket disappears with it
        await board.wait()
        socket_path.unlink(missing_ok=True)
        await asyncio.sleep(1.0)
        assert proxy.returncode is None, "the bridge died with the board"

        board2 = await start_board()  # a fresh controller rebinds the same path
        second = await call(2)
        assert second["id"] == 2 and "content" in second["result"]
        assert proxy.returncode is None
    finally:
        proxy.stdin.close()
        await asyncio.wait_for(proxy.wait(), timeout=15)
        for process in (board, board2):
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
        socket_path.unlink(missing_ok=True)


async def test_manager_mcp_bridge_refuses_instead_of_dying_when_the_board_is_gone(tmp_path):
    """An unreachable board is a refusal the Manager can report, not a lost tool surface."""
    import pytest

    from switchboard.agents.manager_mcp import _Bridge

    bridge = _Bridge(tmp_path / "never-bound.sock", timeout=0.2)
    with pytest.raises(OSError):
        await bridge.exchange('{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n', True)
    await bridge.close()


def test_configured_wrapper_and_environment_are_shared_with_native_runtime(
    session_manager, tmp_path
):
    wrapper = tmp_path / "company-claude"
    wrapper.write_text("#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o700)
    config = Config()
    config.claude.executable = str(wrapper)
    config.claude.env = {"COMPANY_PROXY": "on"}
    session_manager.config = config
    assert session_manager.config.claude.executable == str(wrapper)
    assert session_manager.config.claude.env == {"COMPANY_PROXY": "on"}
