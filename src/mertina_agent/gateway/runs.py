"""Own background runs from history admission through durable terminal publication.

Adapted from Hermes API run ownership and shutdown responsibilities at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-gateway-sessions.md.
Mertina adds bounded retention, session leases and receipt-based commit recovery.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from uuid import uuid4

from mertina_agent.agent.events import (
    AgentEvent,
    RetryScheduled,
    RunCompleted,
    RunFailed,
    RunStopped,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    ToolGenerationStarted,
)
from mertina_agent.config import Settings
from mertina_agent.exceptions import (
    DatabaseError,
    GatewayClosingError,
    RunCapacityError,
    RunNotFoundError,
    SessionConflictError,
    SessionNotFoundError,
    StorageError,
)
from mertina_agent.gateway.run_events import EventBuffer
from mertina_agent.gateway.session import AgentFactory, SessionAgent
from mertina_agent.gateway.turn_lease import Lease, SessionTurnLeaseRegistry
from mertina_agent.state.models import TurnCommit
from mertina_agent.state.serialization import build_commit, fingerprint, turn_status
from mertina_agent.state.store import SessionStore

logger = logging.getLogger(__name__)
MAX_SNAPSHOT_CHARS = 262144
TEXT_EVENT_CHARS = 4096


@dataclass
class RunRecord:
    """A bounded live snapshot, independent of any particular HTTP subscriber."""

    id: str
    session_id: str
    events: EventBuffer
    lease: Lease
    status: str = "pending"
    text: str = ""
    text_truncated: bool = False
    error: str | None = None
    durable: bool = False
    terminal: bool = False
    stop_requested: bool = False
    reconciliation_required: bool = False
    revision: int | None = None
    completed_at: float | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    agent: SessionAgent | None = field(default=None, repr=False)

    def snapshot(self) -> dict[str, object]:
        """Return a copy sufficient to resume display at ``last_event_id``."""
        return {
            "run_id": self.id,
            "session_id": self.session_id,
            "status": self.status,
            "text": self.text,
            "text_truncated": self.text_truncated,
            "error": self.error,
            "durable": self.durable,
            "terminal": self.terminal,
            "stop_requested": self.stop_requested,
            "reconciliation_required": self.reconciliation_required,
            "revision": self.revision,
            "last_event_id": self.events.latest_id,
        }


class RunRegistry:
    """Single-event-loop run admission, ownership, retention and bounded shutdown.

    A run whose commit cannot be reconciled retains its session lease until
    shutdown. Restart after restoring the database loads the durable history;
    the next turn cannot accidentally continue from an unconfirmed prefix.
    """

    def __init__(
        self,
        store: SessionStore,
        agent_factory: AgentFactory,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Borrow the store; own all created agents, tasks and event transports."""
        self.store = store
        self.leases = SessionTurnLeaseRegistry()
        self._factory = agent_factory
        self._settings = settings
        self._clock = clock
        self._runs: dict[str, RunRecord] = {}
        self._closing = False

    def active_run_id(self, session_id: str) -> str | None:
        """Expose a discoverable run identity while execution or commit owns the lease."""
        owner = self.leases.active_owner(session_id)
        return owner if owner in self._runs else None

    def prune(self) -> None:
        """Expire only completed, released runs; active or uncertain work is retained."""
        now = self._clock()
        for identifier, run in tuple(self._runs.items()):
            if (
                run.completed_at is not None
                and now - run.completed_at >= self._settings.gateway_run_ttl_s
                and run.task is None
                and not run.reconciliation_required
            ):
                run.events.close()
                del self._runs[identifier]

    def _admit(self) -> None:
        if self._closing:
            message = "Gateway is shutting down"
            raise GatewayClosingError(message)
        self.prune()
        active = sum(
            run.task is not None or run.reconciliation_required for run in self._runs.values()
        )
        if active >= self._settings.gateway_max_active_runs:
            message = "Active run capacity reached"
            raise RunCapacityError(message)
        while len(self._runs) >= self._settings.gateway_max_runs:
            expired = next(
                (
                    run
                    for run in self._runs.values()
                    if run.task is None and run.terminal and not run.reconciliation_required
                ),
                None,
            )
            if expired is None:
                message = "Run retention capacity reached"
                raise RunCapacityError(message)
            expired.events.close()
            del self._runs[expired.id]

    async def start_run(self, session_id: str, user_message: str) -> RunRecord:
        """Admit under the session lease before reading any durable session state."""
        if (
            not isinstance(user_message, str)
            or not user_message.strip()
            or len(user_message) > 65536
        ):
            message = "Run input must contain 1 to 65536 text characters"
            raise StorageError(message)
        self._admit()
        identifier = uuid4().hex
        lease = self.leases.acquire(session_id, identifier)
        run = RunRecord(
            identifier,
            session_id,
            EventBuffer(
                identifier,
                capacity=self._settings.gateway_event_capacity,
                subscriber_capacity=self._settings.gateway_subscriber_capacity,
                max_subscribers=self._settings.gateway_max_subscribers,
            ),
            lease,
        )
        # Publish a reservation before the first await, so global capacity and
        # session discovery also account for slow admission reads.
        self._runs[identifier] = run
        run.task = asyncio.current_task()
        try:
            await self.store.get_session(session_id)
            if self._closing:
                message = "Gateway is shutting down"
                raise GatewayClosingError(message)
            run.task = asyncio.create_task(
                self._execute(run, user_message), name=f"run-{identifier}"
            )
            run.events.publish("run.accepted", {"status": "pending"})
        except BaseException:
            lease.release()
            run.events.close()
            del self._runs[identifier]
            raise
        return run

    def get_run(self, run_id: str) -> RunRecord:
        """Read active or retained state; process restarts deliberately forget old runs."""
        self.prune()
        run = self._runs.get(run_id)
        if run is None:
            message = "Run does not exist or has expired"
            raise RunNotFoundError(message)
        return run

    def stop_run(self, run_id: str) -> RunRecord:
        """Request cooperative stopping, including before the background task starts."""
        run = self.get_run(run_id)
        if not run.terminal and not run.stop_requested:
            run.stop_requested = True
            if run.agent is not None:
                run.agent.interrupt("Stopped by client")
            run.events.publish("run.stop_requested", {"status": run.status})
        return run

    async def delete_session(self, session_id: str) -> None:
        """Coordinate deletion with admission, then remove retained process caches."""
        if self._closing:
            message = "Gateway is shutting down"
            raise GatewayClosingError(message)
        lease = self.leases.acquire(session_id, uuid4().hex)
        try:
            await self.store.delete_session(session_id)
            for identifier, run in tuple(self._runs.items()):
                if run.session_id == session_id:
                    run.events.close()
                    del self._runs[identifier]
        finally:
            lease.release()

    def _observe(self, run: RunRecord, event: AgentEvent) -> None:
        # Agent terminal callbacks precede the returned result. They cannot
        # acknowledge persistence and must never escape as Gateway terminals.
        if run.terminal or isinstance(event, RunCompleted | RunStopped | RunFailed):
            return
        if isinstance(event, TextDelta):
            if len(run.text) + len(event.text) > MAX_SNAPSHOT_CHARS:
                run.text_truncated = True
                if run.agent is not None:
                    run.agent.interrupt("Gateway output limit reached")
            available = max(0, MAX_SNAPSHOT_CHARS - len(run.text))
            visible = event.text[:available]
            run.text += visible
            for offset in range(0, len(visible), TEXT_EVENT_CHARS):
                run.events.publish(
                    "text.delta", {"text": visible[offset : offset + TEXT_EVENT_CHARS]}
                )
            return
        if isinstance(event, RetryScheduled):
            run.text = ""
            run.text_truncated = False
            run.events.publish("retry.scheduled", asdict(event))
        elif isinstance(event, ToolGenerationStarted):
            run.events.publish("tool.generating", asdict(event))
        elif isinstance(event, ToolCallStarted):
            run.events.publish("tool.started", asdict(event))
        elif isinstance(event, ToolCallFinished):
            run.events.publish("tool.finished", asdict(event))

    async def _persist(self, command: TurnCommit) -> int:
        # Both attempts use exactly the original command. Reconcile after every
        # failed acknowledgement, including the last allowed attempt.
        for attempt in range(2):
            try:
                return (await self.store.commit_turn(command)).revision
            except DatabaseError:
                receipt = await self.store.get_commit_receipt(
                    command.session_id, command.operation_id
                )
                if receipt is not None:
                    if receipt.fingerprint != fingerprint(command):
                        message = "Commit receipt does not match the original operation"
                        raise StorageError(message) from None
                    return receipt.revision
                if attempt == 1:
                    raise
        message = "Commit acknowledgement is unavailable"
        raise StorageError(message)

    def _finish(self, run: RunRecord, status: str, *, error: str | None = None) -> None:
        run.status = status
        run.error = error
        run.terminal = True
        run.completed_at = self._clock()
        run.events.publish(
            f"run.{status}",
            {
                "status": status,
                "durable": run.durable,
                "revision": run.revision,
                "error": error,
                "reconciliation_required": run.reconciliation_required,
            },
        )
        run.events.finish()

    async def _execute(self, run: RunRecord, user_message: str) -> None:
        committing = False
        try:
            if bool(run.stop_requested):
                self._finish(run, "stopped")
                return
            snapshot = await self.store.load_history(run.session_id)
            if run.stop_requested:
                self._finish(run, "stopped")
                return
            run.agent = self._factory(lambda event: self._observe(run, event))
            run.status = "running"
            run.events.publish("run.started", {"status": "running"})
            result = await run.agent.run_conversation(
                user_message, conversation_history=snapshot.messages
            )
            status = turn_status(result)
            command = build_commit(
                snapshot,
                result["messages"],
                operation_id=run.id,
                status=status,
                timestamp_us=time.time_ns() // 1000,
            )
            run.status = "committing"
            run.events.publish("run.committing", {"status": "committing"})
            committing = True
            run.revision = await self._persist(command)
            committing = False
            run.durable = True
            final = result["final_response"] or ""
            run.text = final[:MAX_SNAPSHOT_CHARS]
            run.text_truncated = len(final) > MAX_SNAPSHOT_CHARS
            self._finish(run, status, error="model_failed" if result["failed"] else None)
        except asyncio.CancelledError:
            run.reconciliation_required = committing
            if not run.terminal:
                self._finish(run, "failed", error="shutdown_cancelled")
            raise
        except (SessionConflictError, SessionNotFoundError):
            # A definite rejection, not an unknown outcome: the store wrote
            # nothing because another process changed or removed the session,
            # so the lease is released and no reconciliation is needed.
            logger.error("Run was not saved; the session changed elsewhere: %s", run.id)
            if not run.terminal:
                self._finish(
                    run, "failed", error="history_conflict" if committing else "run_failed"
                )
        except Exception:
            # Task boundary: log only stable identifiers, never chained SQL,
            # model bodies or a traceback that may contain private credentials.
            logger.error("Run failed before durable acknowledgement: %s", run.id)
            run.reconciliation_required = committing
            if not run.terminal:
                self._finish(
                    run, "failed", error="storage_unconfirmed" if committing else "run_failed"
                )
        finally:
            if run.agent is not None:
                try:
                    async with asyncio.timeout(self._settings.gateway_cancel_timeout_s):
                        await run.agent.aclose()
                except (Exception, asyncio.CancelledError):
                    logger.error("Agent resource cleanup failed: %s", run.id)
                run.agent = None
            if not run.reconciliation_required:
                run.lease.release()
            run.task = None

    async def aclose(self) -> None:
        """Stop admission, drain cooperative turns, then cancel within a second budget."""
        self._closing = True
        for run in tuple(self._runs.values()):
            if not run.terminal:
                self.stop_run(run.id)
        tasks = {run.task for run in self._runs.values() if run.task is not None}
        if tasks:
            _, pending = await asyncio.wait(
                tasks, timeout=self._settings.gateway_shutdown_timeout_s
            )
            for task in pending:
                task.cancel()
            if pending:
                _, pending = await asyncio.wait(
                    pending, timeout=self._settings.gateway_cancel_timeout_s
                )
            if pending:
                logger.error("Gateway tasks exceeded the cancellation deadline")
        for run in self._runs.values():
            # Let HTTP handlers drain the terminal and EOF before the runner
            # closes connections. Force-close would discard queued terminals.
            run.events.finish()
            if run.task is None:
                run.lease.release()
