"""Production worker backend: durable native Claude processes under tmux."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from switchboard.agents.attach import Attachment
from switchboard.agents.backend import (
    BackendHealth,
    EventType,
    RuntimeObservation,
    WorkerBusyError,
    WorkerEvent,
    WorkerHandle,
    WorkerNotReadyError,
    WorkerSpec,
)
from switchboard.config import Config
from switchboard.domain.enums import NativeTurnOrigin, NativeTurnStatus, RuntimeProcessState
from switchboard.domain.models import RuntimeHookEvent, now
from switchboard.runtime.native_claude import NativeClaudeRuntime
from switchboard.runtime.supervisor import TmuxRuntimeSupervisor
from switchboard.runtime.tmux import TmuxController, TmuxError, TmuxRuntimeStatus
from switchboard.storage.store import Store

BLOCKED_MARKERS = ("[NEEDS INPUT]", "[NEEDS DECISION]")
MAX_UNIX_SOCKET_PATH_BYTES = 96


def default_tmux_socket_path(state_dir: Path) -> Path:
    """Keep tmux usable when an isolated data directory has a long macOS path."""
    preferred = state_dir / "tmux.sock"
    if len(str(preferred).encode()) <= MAX_UNIX_SOCKET_PATH_BYTES:
        return preferred
    root = Path("/private/tmp") if Path("/private/tmp").is_dir() else Path("/tmp")
    digest = hashlib.sha256(str(state_dir.resolve()).encode()).hexdigest()[:20]
    return root / f"switchboard-tmux-{digest}.sock"


@dataclass
class _NativeSession:
    spec: WorkerSpec
    outbox: asyncio.Queue[tuple[WorkerEvent | None, UUID | None]] = field(
        default_factory=asyncio.Queue
    )
    task: asyncio.Task | None = None
    alive: bool = True
    detail: str = ""
    inflight: set[UUID] = field(default_factory=set)


class NativeClaudeBackend:
    """Maps supported Claude hooks into the orchestration worker-event contract."""

    def __init__(
        self,
        store: Store,
        config: Config,
        state_dir: Path,
        *,
        socket_path: Path | None = None,
        tmux_executable: str | None = None,
    ) -> None:
        executable = tmux_executable or shutil.which("tmux")
        if executable is None:
            raise RuntimeError("tmux is required for native Claude workers.")
        self.store = store
        self.controller = TmuxController(
            socket_path or default_tmux_socket_path(state_dir), executable
        )
        self.supervisor = TmuxRuntimeSupervisor(store, self.controller)
        self.runtime = NativeClaudeRuntime(store, self.supervisor, config, state_dir / "hooks")
        self._sessions: dict[UUID, _NativeSession] = {}

    def launch_fingerprint(self, spec: WorkerSpec) -> str:
        return self.runtime.launch_fingerprint(
            cwd=spec.cwd,
            model=spec.model,
            permission_mode=spec.permission_mode,
            effort=spec.effort,
            session_name=spec.session_name,
            read_only=not spec.writable,
            system_prompt_append=spec.system_prompt_append,
        )

    async def start(self, spec: WorkerSpec) -> WorkerHandle:
        return await self._launch(spec)

    async def resume(self, spec: WorkerSpec) -> WorkerHandle:
        # A missing process is reconstructed as a fresh native session/generation from
        # durable Switchboard state; normal entry never reaches this method.
        return await self._launch(spec)

    async def _launch(self, spec: WorkerSpec) -> WorkerHandle:
        runtime_id = self._runtime_id(spec)
        session = _NativeSession(spec)
        self._sessions[spec.worker_id] = session
        launched = self.runtime.launch(
            runtime_id,
            cwd=spec.cwd,
            model=spec.model,
            permission_mode=spec.permission_mode,
            effort=spec.effort,
            session_name=spec.session_name,
            read_only=not spec.writable,
            system_prompt_append=spec.system_prompt_append,
        )
        session.task = asyncio.create_task(self._watch(spec.worker_id))
        await self._wait_ready(runtime_id)
        if spec.initial_prompt:
            self.runtime.send_managed(runtime_id, spec.initial_prompt)
        return WorkerHandle(
            worker_id=spec.worker_id,
            session_id=launched.expected_session_id,
            runtime_id=runtime_id,
            runtime_generation=spec.runtime_generation,
            adopted=launched.runtime.adopted,
        )

    async def adopt(self, spec: WorkerSpec) -> WorkerHandle:
        runtime_id = self._runtime_id(spec)
        adopted = self.runtime.adopt(
            runtime_id,
            cwd=spec.cwd,
            model=spec.model,
            permission_mode=spec.permission_mode,
            effort=spec.effort,
            session_name=spec.session_name,
            read_only=not spec.writable,
            system_prompt_append=spec.system_prompt_append,
        )
        session = _NativeSession(spec)
        self._sessions[spec.worker_id] = session
        session.task = asyncio.create_task(self._watch(spec.worker_id))
        self._reconcile_completed_lane(runtime_id)
        return WorkerHandle(
            worker_id=spec.worker_id,
            session_id=adopted.runtime.claude_session_id,
            runtime_id=runtime_id,
            runtime_generation=spec.runtime_generation,
            adopted=True,
        )

    async def observe(self, worker_id: UUID) -> RuntimeObservation:
        runtime = self.store.current_runtime(worker_id)
        if runtime is None:
            return RuntimeObservation(exists=False)
        try:
            observed = self.supervisor.observe(runtime.id)
        except TmuxError as exc:
            return RuntimeObservation(exists=False, detail=str(exc))
        status = observed.observation.status
        if status is TmuxRuntimeStatus.STALE:
            return RuntimeObservation(
                exists=True,
                detail=observed.observation.detail or "stale tmux runtime",
            )
        return RuntimeObservation(
            exists=status is TmuxRuntimeStatus.ALIVE,
            runtime_id=runtime.id,
            generation=runtime.generation,
            process_state=observed.runtime.process_state,
            detail=observed.observation.detail,
        )

    async def send(self, worker_id: UUID, message: str) -> None:
        session = self._require(worker_id)
        runtime_id = self._runtime_id(session.spec)
        runtime = self.store.get_runtime(runtime_id)
        if runtime is not None and runtime.process_state in (
            RuntimeProcessState.WAITING,
            RuntimeProcessState.TURN_COMPLETE,
        ):
            self._reconcile_completed_lane(runtime_id, require_delivered=False)
        try:
            self.runtime.send_managed(runtime_id, message)
        except TmuxError as exc:
            if self.store.open_native_turn(runtime_id) is not None:
                raise WorkerBusyError(str(exc)) from exc
            refreshed = self.store.get_runtime(runtime_id)
            # Only a session that has not got there yet is worth retrying. An exited or
            # absent runtime is a real loss and must keep failing closed as a disconnect.
            if refreshed is not None and refreshed.process_state in (
                RuntimeProcessState.STARTING,
                RuntimeProcessState.WAITING,
            ):
                raise WorkerNotReadyError(str(exc)) from exc
            raise

    async def stream(self, worker_id: UUID) -> AsyncIterator[WorkerEvent]:
        session = self._require(worker_id)
        while True:
            event, hook_id = await session.outbox.get()
            if event is None:
                return
            yield event
            # The generator resumes only after SessionManager applied the event.
            turn_id = event.data.get("turn_id")
            if event.type == "result" and turn_id:
                self.runtime.acknowledge(self._runtime_id(session.spec), UUID(turn_id))
            if hook_id is not None:
                self.store.mark_worker_hook_delivered(hook_id)
                session.inflight.discard(hook_id)

    async def interrupt(self, worker_id: UUID) -> None:
        session = self._require(worker_id)
        turn = self.store.open_native_turn(self._runtime_id(session.spec))
        if turn is None:
            return
        self.runtime.interrupt(turn.runtime_id, turn.id)

    def capture(self, worker_id: UUID) -> str:
        session = self._require(worker_id)
        return self.supervisor.capture(self._runtime_id(session.spec))

    async def answer_startup_dialog(self, worker_id: UUID) -> None:
        session = self._require(worker_id)
        self.supervisor.answer_startup_dialog(self._runtime_id(session.spec))

    async def wait_ready(self, worker_id: UUID, timeout: float = 30.0) -> bool:
        session = self._require(worker_id)
        try:
            await self._wait_ready(self._runtime_id(session.spec), timeout=timeout)
        except RuntimeError:
            return False
        return True

    async def stop(self, worker_id: UUID) -> None:
        session = self._sessions.pop(worker_id, None)
        runtime = self.store.current_runtime(worker_id)
        if runtime is not None and runtime.substrate:
            try:
                self.supervisor.terminate(runtime.id)
            except TmuxError:
                pass
        if session is not None:
            session.alive = False
            if session.task is not None:
                session.task.cancel()
            await session.outbox.put((WorkerEvent(worker_id, "stopped", "Session stopped."), None))
            await session.outbox.put((None, None))

    async def health(self, worker_id: UUID) -> BackendHealth:
        observation = await self.observe(worker_id)
        return BackendHealth(alive=observation.exists, detail=observation.detail)

    def attachment(self, spec: WorkerSpec, note: str = "") -> Attachment:
        view = self.runtime.claim_human(self._runtime_id(spec))
        try:
            argv = list(view.argv())
        except Exception:
            self.runtime.release_human(self._runtime_id(spec), composer_cleared=True)
            raise
        runtime = self.store.get_runtime(self._runtime_id(spec))
        return Attachment(
            cwd=spec.cwd,
            session_id=(runtime.claude_session_id if runtime else None) or "",
            argv=argv,
            note=note,
        )

    def release_human(self, worker_id: UUID, *, composer_cleared: bool) -> None:
        # Resolved from durable state, exactly as `attachment` claims it. Requiring a live
        # session controller here made leaving harder than entering: a worker whose
        # controller had gone -- a disconnected one, or any worker after a board restart --
        # could be entered and then never released, stranding ownership on the human and
        # its run paused with no way back.
        runtime = self.store.current_runtime(worker_id)
        if runtime is None:
            raise KeyError(f"Worker {worker_id} has no durable runtime to release.")
        runtime_id = runtime.id
        self.runtime.release_human(runtime_id, composer_cleared=composer_cleared)
        turns = self.store.list_native_turns(runtime_id)
        if (
            turns
            and turns[-1].status is NativeTurnStatus.PENDING
            and turns[-1].human_intervened
        ):
            turns[-1].status = NativeTurnStatus.INTERRUPTED
            turns[-1].error = "Human reconciled uncertain delivery and cleared the composer."
            turns[-1].updated_at = now()
            self.store.save_native_turn(turns[-1])
        refreshed = self.store.get_runtime(runtime_id)
        if (
            turns
            and turns[-1].status is not NativeTurnStatus.INTERRUPTED
            and refreshed is not None
            and refreshed.process_state is RuntimeProcessState.TURN_COMPLETE
            and self.runtime.completed(turns[-1].id) is not None
        ):
            self.runtime.acknowledge(runtime_id, turns[-1].id)

    async def _wait_ready(self, runtime_id: UUID, timeout: float = 60.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            runtime = self.store.get_runtime(runtime_id)
            if runtime is not None and runtime.process_state is RuntimeProcessState.READY:
                return
            if runtime is not None and runtime.process_state is RuntimeProcessState.EXITED:
                raise RuntimeError("Native Claude exited before SessionStart.")
            await asyncio.sleep(0.05)
        raise RuntimeError("Timed out waiting for native Claude SessionStart.")

    async def _watch(self, worker_id: UUID) -> None:
        session = self._require(worker_id)
        runtime_id = self._runtime_id(session.spec)
        loop = asyncio.get_running_loop()
        next_observation = loop.time()
        try:
            while session.alive:
                pending = self.store.pending_worker_hook_events(runtime_id)
                for hook in pending:
                    if hook.id in session.inflight:
                        continue
                    event = self._worker_event(worker_id, hook)
                    if event is None:
                        self.store.mark_worker_hook_delivered(hook.id)
                    else:
                        session.inflight.add(hook.id)
                        await session.outbox.put((event, hook.id))
                if loop.time() >= next_observation:
                    observed = self.supervisor.observe(runtime_id)
                    if observed.observation.status is not TmuxRuntimeStatus.ALIVE:
                        session.alive = False
                        detail = observed.observation.detail or observed.observation.status.value
                        await session.outbox.put(
                            (WorkerEvent(worker_id, "failed", f"Native runtime {detail}."), None)
                        )
                        await session.outbox.put((None, None))
                        return
                    next_observation = loop.time() + 0.5
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            session.alive = False
            session.detail = str(exc)
            await session.outbox.put((WorkerEvent(worker_id, "failed", str(exc)), None))

    def _worker_event(self, worker_id: UUID, hook: RuntimeHookEvent) -> WorkerEvent | None:
        turn = self.store.get_native_turn(hook.turn_id) if hook.turn_id else None
        managed = (
            turn is not None
            and turn.origin is NativeTurnOrigin.MANAGED
            and not turn.human_intervened
        )
        payload = hook.payload
        data: dict = {"hook_event_id": str(hook.id)}
        if turn is not None:
            data["turn_id"] = str(turn.id)
        if hook.event_name == "SessionStart":
            return WorkerEvent(worker_id, "session", hook.session_id or "", data)
        if hook.event_name == "PreToolUse" and managed:
            data["input"] = payload.get("tool_input", {})
            return WorkerEvent(worker_id, "tool", str(payload.get("tool_name") or "tool"), data)
        if (
            hook.event_name == "PermissionRequest"
            or (
                hook.event_name == "Notification"
                and payload.get("notification_type")
                in ("permission_prompt", "elicitation_dialog")
            )
        ) and managed:
            return WorkerEvent(
                worker_id,
                "permission",
                f"Permission required for {payload.get('tool_name') or 'tool'}.",
                data,
            )
        if hook.event_name in ("Stop", "StopFailure") and managed:
            text = str(payload.get("last_assistant_message") or "")
            data["is_error"] = hook.event_name == "StopFailure"
            data["final_only"] = True
            kind: EventType = (
                "result"
                if hook.event_name == "StopFailure"
                else "blocked"
                if _looks_blocked(text)
                else "result"
            )
            return WorkerEvent(worker_id, kind, text, data)
        if hook.event_name == "SessionEnd":
            return WorkerEvent(worker_id, "stopped", str(payload.get("reason") or "Session ended"), data)
        return None

    def _runtime_id(self, spec: WorkerSpec) -> UUID:
        if spec.runtime_id is None:
            raise RuntimeError("Native workers require a durable runtime id.")
        return spec.runtime_id

    def _require(self, worker_id: UUID) -> _NativeSession:
        session = self._sessions.get(worker_id)
        if session is None:
            raise KeyError(f"Worker {worker_id} has no native session controller.")
        return session

    def _reconcile_completed_lane(
        self, runtime_id: UUID, *, require_delivered: bool = True
    ) -> None:
        turns = self.store.list_native_turns(runtime_id)
        if not turns or self.runtime.completed(turns[-1].id) is None:
            return
        if require_delivered:
            terminal = [
                event
                for event in self.store.runtime_hook_events(runtime_id)
                if event.turn_id == turns[-1].id
                and event.event_name in ("Stop", "StopFailure")
            ]
            if terminal and not self.store.worker_hook_delivered(terminal[-1].id):
                return
        runtime = self.store.get_runtime(runtime_id)
        if runtime is not None and runtime.process_state in (
            RuntimeProcessState.TURN_COMPLETE,
            RuntimeProcessState.WAITING,
        ):
            self.runtime.acknowledge(runtime_id, turns[-1].id)


def _looks_blocked(text: str) -> bool:
    upper = text.upper()
    return any(marker in upper for marker in BLOCKED_MARKERS)
