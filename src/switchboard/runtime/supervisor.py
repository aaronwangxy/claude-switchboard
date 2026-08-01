"""Durable RuntimeInstance integration for persistent process substrates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from switchboard.domain.enums import RuntimeOwner, RuntimeProcessState
from switchboard.domain.models import RuntimeInstance, now
from switchboard.runtime.tmux import (
    RuntimeBinding,
    TmuxController,
    TmuxError,
    TmuxObservation,
    TmuxRuntimeStatus,
    TmuxTarget,
    TmuxView,
)
from switchboard.storage.store import Store


@dataclass(frozen=True)
class SupervisedRuntime:
    runtime: RuntimeInstance
    observation: TmuxObservation
    adopted: bool = False


class TmuxRuntimeSupervisor:
    """Bind durable runtime generations to exact tmux targets."""

    def __init__(self, store: Store, controller: TmuxController) -> None:
        self.store = store
        self.controller = controller

    def launch(
        self,
        runtime_id: UUID,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> SupervisedRuntime:
        runtime = self._runtime(runtime_id)
        binding = self._binding(runtime)
        expected = self._target(runtime, required=False)
        if expected is not None:
            observation = self.controller.observe(binding, expected)
            if observation.status is TmuxRuntimeStatus.ALIVE:
                return self._record(runtime, observation, adopted=True)
            raise TmuxError(
                f"Runtime generation {runtime.generation} already has a durable tmux target "
                f"that is {observation.status.value}; create a new generation instead."
            )

        # Even without durable substrate fields, refuse a same-name stale or duplicate
        # session. TmuxController.create performs the same check atomically on races.
        discovered = self.controller.observe(binding, None)
        if discovered.status is TmuxRuntimeStatus.ALIVE:
            raise TmuxError("A tmux runtime exists without matching durable target identity.")
        if discovered.status is TmuxRuntimeStatus.STALE:
            raise TmuxError("A stale tmux runtime occupies this runtime identity.")
        target = self.controller.create(binding, command, cwd=cwd, env=env)
        runtime.substrate = target.as_substrate()
        runtime.process_state = RuntimeProcessState.READY
        runtime.owner = RuntimeOwner.MANAGER
        runtime.updated_at = now()
        self.store.save_runtime(runtime)
        observation = self.controller.observe(binding, target)
        return SupervisedRuntime(runtime, observation, adopted=False)

    def observe(self, runtime_id: UUID) -> SupervisedRuntime:
        runtime = self._runtime(runtime_id)
        target = self._required_target(runtime)
        observation = self.controller.observe(self._binding(runtime), target)
        return self._record(runtime, observation)

    def send(self, runtime_id: UUID, text: str) -> None:
        runtime = self._runtime(runtime_id)
        if runtime.owner is not RuntimeOwner.MANAGER:
            raise TmuxError("Runtime input is human-controlled; programmatic input is refused.")
        self.controller.send_literal(self._binding(runtime), self._required_target(runtime), text)

    def set_owner(self, runtime_id: UUID, owner: RuntimeOwner) -> RuntimeInstance:
        runtime = self._runtime(runtime_id)
        self.controller.set_owner(self._binding(runtime), self._required_target(runtime), owner)
        runtime.owner = owner
        runtime.updated_at = now()
        return self.store.save_runtime(runtime)

    def view(self, runtime_id: UUID) -> TmuxView:
        runtime = self._runtime(runtime_id)
        if runtime.owner is not RuntimeOwner.HUMAN:
            raise TmuxError("Claim human ownership before entering this runtime.")
        return self.controller.view(self._binding(runtime), self._required_target(runtime))

    def _record(
        self, runtime: RuntimeInstance, observation: TmuxObservation, *, adopted: bool = False
    ) -> SupervisedRuntime:
        if observation.status is TmuxRuntimeStatus.ALIVE:
            if observation.owner is not None:
                # Tmux metadata survives a Python/controller restart and is the substrate's
                # authoritative reconstruction of who may write input.
                runtime.owner = observation.owner
        elif observation.status is TmuxRuntimeStatus.EXITED:
            runtime.process_state = RuntimeProcessState.EXITED
        elif observation.status is TmuxRuntimeStatus.ABSENT:
            runtime.process_state = RuntimeProcessState.ABSENT
        elif observation.status is TmuxRuntimeStatus.STALE:
            runtime.process_state = RuntimeProcessState.WAITING
        runtime.updated_at = now()
        self.store.save_runtime(runtime)
        return SupervisedRuntime(runtime, observation, adopted=adopted)

    def _runtime(self, runtime_id: UUID) -> RuntimeInstance:
        runtime = self.store.get_runtime(runtime_id)
        if runtime is None:
            raise TmuxError(f"Runtime {runtime_id} does not exist in durable state.")
        return runtime

    @staticmethod
    def _binding(runtime: RuntimeInstance) -> RuntimeBinding:
        return RuntimeBinding(runtime.id, runtime.generation, runtime.launch_fingerprint)

    @staticmethod
    def _target(runtime: RuntimeInstance, *, required: bool = True) -> TmuxTarget | None:
        if not runtime.substrate:
            if required:
                raise TmuxError("Runtime has no durable tmux target identity.")
            return None
        return TmuxTarget.from_substrate(runtime.substrate)

    @classmethod
    def _required_target(cls, runtime: RuntimeInstance) -> TmuxTarget:
        target = cls._target(runtime)
        assert target is not None
        return target
