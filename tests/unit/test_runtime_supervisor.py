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
    """A tmux controller that can run a callback the instant the process 'starts'."""

    def __init__(self, store: Store, on_create=None) -> None:
        self.store = store
        self.on_create = on_create
        self.created: list[tuple] = []

    def create(self, binding: RuntimeBinding, command, *, cwd: Path, env=None) -> TmuxTarget:
        self.created.append((binding.runtime_id, tuple(command)))
        if self.on_create is not None:
            self.on_create()
        return TARGET

    def observe(self, binding: RuntimeBinding, target: TmuxTarget | None) -> TmuxObservation:
        if target is None:
            return TmuxObservation(status=TmuxRuntimeStatus.ABSENT)
        return TmuxObservation(
            status=TmuxRuntimeStatus.ALIVE, target=target, owner=RuntimeOwner.MANAGER
        )


@pytest.fixture
def supervised(store: Store):
    def build(
        on_create=None, state: RuntimeProcessState = RuntimeProcessState.ABSENT
    ) -> tuple[TmuxRuntimeSupervisor, UUID]:
        controller = FakeController(store, on_create)
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
        hook_store = Store(store.path)
        try:
            runtime = hook_store.get_runtime(runtime_id)
            runtime.process_state = RuntimeProcessState.READY
            hook_store.save_runtime(runtime)
        finally:
            hook_store.close()

    supervisor, runtime_id = supervised(session_start)

    supervisor.launch(runtime_id, ("claude",), cwd=tmp_path)

    stored = store.get_runtime(runtime_id)
    assert stored.process_state is RuntimeProcessState.READY, (
        "the supervisor wrote its pre-launch snapshot back over the hook's SessionStart"
    )
    assert stored.substrate == TARGET.as_substrate(), "the tmux target must still be bound"
