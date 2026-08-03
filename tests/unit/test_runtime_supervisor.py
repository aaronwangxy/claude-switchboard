"""Binding a durable runtime generation to a tmux target.

Deterministic: the tmux controller is faked, because what these tests are about is the
supervisor's own writes racing Claude's command hooks. The hook bridge runs in a separate
process the moment Claude starts, so every persist the supervisor performs after the
process exists must be a read-modify-write, never a write-back of a pre-launch snapshot.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from switchboard.domain.enums import (
    RuntimeAgentKind,
    RuntimeOwner,
    RuntimeProcessState,
)
from switchboard.domain.models import RuntimeInstance
from switchboard.runtime.supervisor import TmuxRuntimeSupervisor
from switchboard.runtime.tmux import (
    RuntimeBinding,
    TmuxObservation,
    TmuxRuntimeStatus,
    TmuxTarget,
)
from switchboard.storage.store import Store

TARGET = TmuxTarget(session_name="switchboard-test", pane_id="%1", pane_pid=4242)


class FakeController:
    """A tmux controller that can run a callback during any subprocess round trip.

    Every mutating tmux call is a subprocess, and Claude's hooks write to the same
    database throughout. `during` stands for whatever the session's own process commits
    while the supervisor is waiting on tmux.
    """

    def __init__(self, store: Store, during=None) -> None:
        self.store = store
        self.during = during
        self.created: list[tuple] = []

    def create(self, binding: RuntimeBinding, command, *, cwd: Path, env=None) -> TmuxTarget:
        self.created.append((binding.runtime_id, tuple(command)))
        self._round_trip()
        return TARGET

    def observe(self, binding: RuntimeBinding, target: TmuxTarget | None) -> TmuxObservation:
        if target is None:
            return TmuxObservation(status=TmuxRuntimeStatus.ABSENT)
        return TmuxObservation(
            status=TmuxRuntimeStatus.ALIVE, target=target, owner=RuntimeOwner.MANAGER
        )

    def set_owner(self, binding: RuntimeBinding, target: TmuxTarget, owner: RuntimeOwner) -> None:
        self._round_trip()

    def terminate(self, binding: RuntimeBinding, target: TmuxTarget) -> None:
        self._round_trip()

    def _round_trip(self) -> None:
        if self.during is not None:
            self.during()


def write_from_the_session(store: Store, runtime_id: UUID, **fields) -> None:
    """Commit a hook's state change from a second connection, as the bridge does."""
    hook_store = Store(store.path)
    try:
        runtime = hook_store.get_runtime(runtime_id)
        for name, value in fields.items():
            setattr(runtime, name, value)
        hook_store.save_runtime(runtime)
    finally:
        hook_store.close()


@pytest.fixture
def supervised(store: Store):
    def build(
        during=None, state: RuntimeProcessState = RuntimeProcessState.ABSENT
    ) -> tuple[TmuxRuntimeSupervisor, UUID]:
        controller = FakeController(store, during)
        runtime = store.save_runtime(
            RuntimeInstance(
                agent_id=uuid4(),
                agent_kind=RuntimeAgentKind.WORKER,
                backend="native-claude",
                launch_fingerprint="sha256:test",
                process_state=state,
            )
        )
        return TmuxRuntimeSupervisor(store, controller), runtime.id  # type: ignore[arg-type]

    return build


@pytest.mark.parametrize(
    "state", [RuntimeProcessState.ABSENT, RuntimeProcessState.STARTING]
)
def test_launch_binds_the_tmux_target_and_reports_starting(supervised, store, tmp_path, state):
    supervisor, runtime_id = supervised(state=state)

    supervisor.launch(runtime_id, ("claude",), cwd=tmp_path)

    stored = store.get_runtime(runtime_id)
    assert stored.substrate == TARGET.as_substrate()
    assert stored.process_state is RuntimeProcessState.STARTING
    assert stored.owner is RuntimeOwner.MANAGER


def test_launch_does_not_undo_a_session_start_that_arrived_while_it_was_binding(
    supervised, store, tmp_path
):
    """The regression: a fast SessionStart must survive the supervisor's own write.

    Claude's SessionStart hook writes READY from its own process, and it can land between
    tmux reporting the pane and the supervisor persisting that pane. Writing back the
    pre-launch snapshot lost that transition permanently -- SessionStart fires once -- so
    the controller waited out its readiness timeout, declared a perfectly healthy session
    blocked on "workspace trust, login, or another startup prompt", and never delivered
    the prompt the session was started for.
    """
    def session_start() -> None:
        # Exactly what runtime.hook_bridge.handle_hook does, from its own process.
        write_from_the_session(store, runtime_id, process_state=RuntimeProcessState.READY)

    supervisor, runtime_id = supervised(session_start)

    supervisor.launch(runtime_id, ("claude",), cwd=tmp_path)

    stored = store.get_runtime(runtime_id)
    assert stored.process_state is RuntimeProcessState.READY, (
        "the supervisor wrote its pre-launch snapshot back over the hook's SessionStart"
    )
    assert stored.substrate == TARGET.as_substrate(), "the tmux target must still be bound"


def test_handing_a_session_to_the_user_keeps_the_turn_it_finished_meanwhile(
    supervised, store, tmp_path
):
    """Entering a busy worker must not rewind the turn it completed as you arrived.

    Ctrl+E hands tmux ownership to the user, which is a tmux round trip; Claude's Stop
    hook writes TURN_COMPLETE from its own process. Writing back the snapshot read before
    that round trip restored TURN_ACTIVE -- and a turn that never completes is a worker
    the Manager can neither send to nor consider free, permanently.
    """
    supervisor, runtime_id = supervised()
    supervisor.launch(runtime_id, ("claude",), cwd=tmp_path)
    write_from_the_session(store, runtime_id, process_state=RuntimeProcessState.TURN_ACTIVE)
    supervisor.controller.during = lambda: write_from_the_session(
        store, runtime_id, process_state=RuntimeProcessState.TURN_COMPLETE
    )

    supervisor.set_owner(runtime_id, RuntimeOwner.HUMAN)

    stored = store.get_runtime(runtime_id)
    assert stored.owner is RuntimeOwner.HUMAN, "the user must own the session they entered"
    assert stored.process_state is RuntimeProcessState.TURN_COMPLETE, (
        "handing over ownership wrote a stale snapshot over the session's own Stop hook"
    )


def test_terminating_a_session_records_the_exit_without_losing_its_identity(
    supervised, store, tmp_path
):
    """Killing the pane is another round trip, and SessionEnd lands during it."""
    supervisor, runtime_id = supervised()
    supervisor.launch(runtime_id, ("claude",), cwd=tmp_path)
    supervisor.controller.during = lambda: write_from_the_session(
        store, runtime_id, claude_session_id="337a9be9-32ba-4079-979f-8c05d76d0f1b"
    )

    supervisor.terminate(runtime_id)

    stored = store.get_runtime(runtime_id)
    assert stored.process_state is RuntimeProcessState.EXITED, "termination is what happened"
    assert stored.claude_session_id == "337a9be9-32ba-4079-979f-8c05d76d0f1b", (
        "terminating wrote a stale snapshot over the session's own final hook"
    )
