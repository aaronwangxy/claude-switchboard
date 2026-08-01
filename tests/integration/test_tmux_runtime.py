"""Real tmux substrate tests; no Claude process or API is involved."""

from __future__ import annotations

import json
import os
import pty
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from switchboard.domain.enums import RuntimeAgentKind, RuntimeOwner, RuntimeProcessState
from switchboard.domain.models import RuntimeInstance
from switchboard.runtime.supervisor import TmuxRuntimeSupervisor
from switchboard.runtime.tmux import (
    RuntimeBinding,
    TmuxController,
    TmuxError,
    TmuxRuntimeStatus,
    TmuxTarget,
)
from switchboard.storage.store import Store

FAKE = Path(__file__).parents[1] / "fixtures" / "fake_interactive.py"


def wait_for(check: Callable[[], object], timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition did not become true")


def events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def tmux_controller(tmp_path: Path) -> Iterator[TmuxController]:
    executable = shutil.which("tmux")
    if executable is None:
        pytest.skip("tmux is not installed")
    # Unix-domain socket paths are short (roughly 104 bytes on macOS); pytest's full
    # temporary directory can exceed that before the filename is added.
    socket = Path("/private/tmp") / f"switchboard-test-{uuid4().hex}.sock"
    controller = TmuxController(socket, executable)
    yield controller
    subprocess.run(
        [executable, "-S", str(socket), "kill-server"],
        capture_output=True,
        check=False,
    )


@pytest.fixture
def runtime(store: Store) -> RuntimeInstance:
    instance = RuntimeInstance(
        agent_id=uuid4(),
        agent_kind=RuntimeAgentKind.WORKER,
        generation=3,
        backend="tmux-prototype",
        launch_fingerprint="sha256:prototype",
    )
    return store.save_runtime(instance)


@pytest.fixture
def launched(
    store: Store, runtime: RuntimeInstance, tmux_controller: TmuxController, tmp_path: Path
):
    log = tmp_path / "fake.jsonl"
    supervisor = TmuxRuntimeSupervisor(store, tmux_controller)
    result = supervisor.launch(
        runtime.id,
        [sys.executable, "-u", str(FAKE), str(log)],
        cwd=tmp_path,
    )
    wait_for(lambda: events(log))
    return supervisor, result.runtime, result.observation.target, log


def test_runtime_survives_controller_restart_and_is_adopted_without_duplication(
    launched, store: Store, tmux_controller: TmuxController
):
    supervisor, runtime, target, log = launched
    original_pid = events(log)[0]["pid"]

    restarted = TmuxRuntimeSupervisor(
        store, TmuxController(tmux_controller.socket_path, tmux_controller.executable)
    )
    observed = restarted.observe(runtime.id)
    adopted = restarted.launch(
        runtime.id,
        [sys.executable, "-c", "raise SystemExit('must not launch')"],
        cwd=log.parent,
    )

    assert observed.observation.status is TmuxRuntimeStatus.ALIVE
    assert observed.observation.target == target
    assert adopted.adopted
    assert adopted.observation.target.pane_pid == target.pane_pid == original_pid
    assert [event for event in events(log) if event["event"] == "started"] == [events(log)[0]]


def test_generation_fingerprint_and_exact_pane_identity_are_all_required(launched):
    supervisor, runtime, target, log = launched
    controller = supervisor.controller

    wrong_generation = RuntimeBinding(runtime.id, runtime.generation + 1, runtime.launch_fingerprint)
    wrong_fingerprint = RuntimeBinding(runtime.id, runtime.generation, "different")
    wrong_target = TmuxTarget(target.session_name, "%99999", target.pane_pid)

    assert controller.observe(wrong_generation, target).status is TmuxRuntimeStatus.STALE
    assert controller.observe(wrong_fingerprint, target).status is TmuxRuntimeStatus.STALE
    assert controller.observe(RuntimeBinding(runtime.id, runtime.generation, runtime.launch_fingerprint), wrong_target).status is TmuxRuntimeStatus.STALE


def test_literal_input_is_one_multiline_unicode_turn_with_metacharacters(launched):
    supervisor, runtime, target, log = launched
    message = "first line\nsecond ‘Unicode’ line\n'\"; $(touch /tmp/never) | & < > `nope`"

    supervisor.send(runtime.id, message)
    turn = wait_for(lambda: next((e for e in events(log) if e["event"] == "turn"), None))

    assert turn["text"] == message
    assert turn["pid"] == target.pane_pid


def test_owner_is_reconstructed_and_human_control_refuses_programmatic_input(
    launched, store: Store, tmux_controller: TmuxController
):
    supervisor, runtime, target, log = launched
    supervisor.set_owner(runtime.id, RuntimeOwner.HUMAN)

    # Simulate stale Python memory after a controller restart. Tmux metadata restores it.
    stored = store.get_runtime(runtime.id)
    stored.owner = RuntimeOwner.MANAGER
    store.save_runtime(stored)
    restarted = TmuxRuntimeSupervisor(
        store, TmuxController(tmux_controller.socket_path, tmux_controller.executable)
    )
    observed = restarted.observe(runtime.id)

    assert observed.runtime.owner is RuntimeOwner.HUMAN
    with pytest.raises(TmuxError, match="human-controlled"):
        restarted.send(runtime.id, "must not arrive")
    assert not any(e.get("text") == "must not arrive" for e in events(log))


def test_external_view_attaches_to_same_process_and_detach_leaves_it_alive(launched):
    supervisor, runtime, target, log = launched
    supervisor.set_owner(runtime.id, RuntimeOwner.HUMAN)
    view = supervisor.view(runtime.id)
    master, slave = pty.openpty()
    environment = dict(os.environ, TERM="xterm-256color")
    viewer = subprocess.Popen(
        view.external_argv,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=environment,
        start_new_session=True,
    )
    os.close(slave)
    try:
        attached = wait_for(
            lambda: supervisor.observe(runtime.id).observation.attached_clients == 1
        )
        assert attached
        assert viewer.poll() is None
        assert supervisor.observe(runtime.id).observation.target.pane_pid == target.pane_pid

        # The Python control plane is still executing while another client views the pane.
        control_plane_marker = log.parent / "control-plane-alive"
        control_plane_marker.write_text("yes")
        assert control_plane_marker.read_text() == "yes"

        os.write(master, b"\x02d")  # tmux prefix Ctrl-B, then detach-client
        wait_for(lambda: viewer.poll() is not None)
    finally:
        if viewer.poll() is None:
            viewer.terminate()
            viewer.wait(timeout=5)
        os.close(master)

    observation = supervisor.observe(runtime.id).observation
    assert observation.status is TmuxRuntimeStatus.ALIVE
    assert observation.target.pane_pid == target.pane_pid
    assert observation.attached_clients == 0


def test_exited_and_absent_are_distinct(launched):
    supervisor, runtime, target, log = launched
    supervisor.send(runtime.id, "__exit__")
    exited = wait_for(
        lambda: (
            observation
            if (observation := supervisor.observe(runtime.id).observation).status
            is TmuxRuntimeStatus.EXITED
            else None
        )
    )
    assert exited.exit_status == 7
    assert store_state(supervisor, runtime.id) is RuntimeProcessState.EXITED

    subprocess.run(
        [
            supervisor.controller.executable,
            "-S",
            str(supervisor.controller.socket_path),
            "kill-session",
            "-t",
            target.session_name,
        ],
        check=True,
    )
    absent = supervisor.observe(runtime.id).observation
    assert absent.status is TmuxRuntimeStatus.ABSENT
    assert store_state(supervisor, runtime.id) is RuntimeProcessState.ABSENT


def store_state(supervisor: TmuxRuntimeSupervisor, runtime_id):
    return supervisor.store.get_runtime(runtime_id).process_state
