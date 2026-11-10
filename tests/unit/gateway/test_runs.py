import asyncio
from collections import deque
from dataclasses import replace

import pytest

from mertina_agent.agent.events import (
    RetryScheduled,
    RunCompleted,
    RunFailed,
    RunStopped,
    TextDelta,
)
from mertina_agent.agent.transports.types import Usage
from mertina_agent.config import Settings
from mertina_agent.exceptions import (
    DatabaseError,
    GatewayClosingError,
    RunCapacityError,
    RunNotFoundError,
    SessionConflictError,
    SessionNotFoundError,
)
from mertina_agent.gateway.runs import MAX_SNAPSHOT_CHARS, RunRegistry
from mertina_agent.state.models import CommitReceipt, HistorySnapshot, SessionRecord
from mertina_agent.state.serialization import decode_message, fingerprint


class FakeStore:
    def __init__(self):
        self.sessions = {}
        self.histories = {}
        self.receipts = {}
        self.get_calls = []
        self.load_calls = []
        self.commands = []
        self.commit_outcomes = deque()
        self.get_entered = asyncio.Event()
        self.load_entered = asyncio.Event()
        self.commit_entered = asyncio.Event()
        self.get_gate = None
        self.load_gate = None
        self.commit_gate = None
        self.receipt_error = False
        self.corrupt_receipt = False

    def add_session(self):
        identifier = f"{len(self.sessions) + 1:032x}"
        self.sessions[identifier] = SessionRecord(identifier, "Conversation", 1, 1, 0, 0, 0)
        self.histories[identifier] = ()
        return identifier

    async def get_session(self, session_id):
        self.get_calls.append(session_id)
        self.get_entered.set()
        if self.get_gate is not None:
            await self.get_gate.wait()
        if session_id not in self.sessions:
            message = "Session does not exist"
            raise SessionNotFoundError(message)
        return self.sessions[session_id]

    async def load_history(self, session_id):
        self.load_calls.append(session_id)
        self.load_entered.set()
        if self.load_gate is not None:
            await self.load_gate.wait()
        return HistorySnapshot(self.sessions[session_id], self.histories[session_id])

    async def commit_turn(self, command):
        self.commands.append(command)
        self.commit_entered.set()
        if self.commit_gate is not None:
            await self.commit_gate.wait()
        outcome = self.commit_outcomes.popleft() if self.commit_outcomes else "ok"
        if outcome == "before":
            kind = "unknown"
            raise DatabaseError(kind)
        key = (command.session_id, command.operation_id)
        receipt = self.receipts.get(key)
        if receipt is None:
            session = self.sessions[command.session_id]
            assert session.history_revision == command.expected_revision
            assert session.message_count == command.expected_count
            messages = tuple(decode_message(payload) for payload in command.payloads)
            self.histories[command.session_id] += messages
            self.sessions[command.session_id] = replace(
                session,
                history_revision=session.history_revision + 1,
                message_count=session.message_count + len(messages),
            )
            receipt = CommitReceipt(
                command.session_id,
                command.operation_id,
                "incorrect" if self.corrupt_receipt else fingerprint(command),
                command.expected_revision + 1,
                command.expected_count + 1,
                command.expected_count + len(messages),
                command.status,
            )
            self.receipts[key] = receipt
        if outcome == "after":
            kind = "unknown"
            raise DatabaseError(kind)
        return receipt

    async def get_commit_receipt(self, session_id, operation_id):
        if self.receipt_error:
            kind = "connection"
            raise DatabaseError(kind)
        return self.receipts.get((session_id, operation_id))

    async def delete_session(self, session_id):
        self.sessions.pop(session_id)
        self.histories.pop(session_id)


class FakeAgent:
    def __init__(self, callback, *, blocked=False, cooperative=True, outcome="completed"):
        self.callback = callback
        self.cooperative = cooperative
        self.outcome = outcome
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.interrupts = []
        self.interrupted = False
        self.closed = False
        self.cancelled = False
        self.history = None
        self.failure = None

    async def run_conversation(self, user_message, *, conversation_history=None):
        self.history = tuple(conversation_history or ())
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.failure is not None:
            raise self.failure
        outcome = "stopped" if self.interrupted else self.outcome
        answer = "partial answer" if outcome == "stopped" else "answer"
        messages = [
            *self.history,
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": answer},
        ]
        self.callback(TextDelta(answer))
        if outcome == "stopped":
            self.callback(RunStopped(answer, "interrupted"))
        elif outcome == "failed":
            self.callback(RunFailed("provider failed", answer, "error"))
        else:
            self.callback(RunCompleted(answer, outcome == "completed", outcome))
        return {
            "messages": messages,
            "final_response": answer,
            "api_calls": 1,
            "completed": outcome == "completed",
            "failed": outcome == "failed",
            "interrupted": outcome == "stopped",
            "partial": outcome == "partial",
            "turn_exit_reason": outcome,
            "usage": Usage(),
        }

    def interrupt(self, reason=None):
        self.interrupts.append(reason)
        if self.cooperative:
            self.interrupted = True
            self.release.set()
        return self.cooperative

    async def aclose(self):
        self.closed = True


class FakeFactory:
    def __init__(self, **options):
        self.options = options
        self.agents = []

    def __call__(self, callback):
        agent = FakeAgent(callback, **self.options)
        self.agents.append(agent)
        return agent


def settings(**options):
    return Settings(
        _env_file=None,
        gateway_shutdown_timeout_s=0.02,
        gateway_cancel_timeout_s=0.1,
        **options,
    )


async def complete(run):
    task = run.task
    if task is not None:
        async with asyncio.timeout(2):
            await asyncio.gather(task, return_exceptions=True)
    assert run.terminal
    assert run.task is None


def drain(subscription):
    events = []
    while not subscription.queue.empty():
        event = subscription.queue.get_nowait()
        if event is not None:
            events.append(event)
    return events


def run_scenario(scenario):
    async def bounded():
        async with asyncio.timeout(2):
            await scenario()

    asyncio.run(bounded())


def test_same_session_is_reserved_before_admission_or_history_read():
    async def scenario():
        store, factory = FakeStore(), FakeFactory()
        session = store.add_session()
        store.get_gate = asyncio.Event()
        registry = RunRegistry(store, factory, settings())
        starting = asyncio.create_task(registry.start_run(session, "first"))
        await store.get_entered.wait()
        assert registry.active_run_id(session) is not None
        with pytest.raises(SessionConflictError):
            await registry.start_run(session, "second")
        with pytest.raises(SessionConflictError):
            await registry.delete_session(session)
        assert store.get_calls == [session]
        assert store.load_calls == []
        store.get_gate.set()
        run = await starting
        await complete(run)
        assert registry.active_run_id(session) is None
        await registry.aclose()

    run_scenario(scenario)


def test_global_capacity_counts_admission_waiting_on_storage():
    async def scenario():
        store, factory = FakeStore(), FakeFactory()
        first, second = store.add_session(), store.add_session()
        store.get_gate = asyncio.Event()
        registry = RunRegistry(store, factory, settings(gateway_max_active_runs=1))
        starting = asyncio.create_task(registry.start_run(first, "first"))
        await store.get_entered.wait()
        with pytest.raises(RunCapacityError):
            await registry.start_run(second, "second")
        assert store.get_calls == [first]
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        assert registry.active_run_id(first) is None
        store.get_gate.set()
        await complete(await registry.start_run(second, "replacement"))
        await registry.aclose()

    run_scenario(scenario)


def test_different_sessions_execute_concurrently_and_close_owned_agents():
    async def scenario():
        store, factory = FakeStore(), FakeFactory(blocked=True)
        registry = RunRegistry(store, factory, settings())
        first = await registry.start_run(store.add_session(), "first")
        second = await registry.start_run(store.add_session(), "second")
        await store.load_entered.wait()
        while len(factory.agents) < 2:
            await asyncio.sleep(0)
        assert first.status == second.status == "running"
        assert first.agent is not second.agent
        for agent in factory.agents:
            agent.release.set()
        await asyncio.gather(complete(first), complete(second))
        assert all(agent.closed for agent in factory.agents)
        assert first.agent is None
        assert second.agent is None
        await registry.aclose()

    run_scenario(scenario)


@pytest.mark.parametrize("outcome", ["completed", "stopped", "failed", "partial"])
def test_agent_terminal_is_withheld_until_durable_commit_and_lease_release(outcome):
    async def scenario():
        store, factory = FakeStore(), FakeFactory(outcome=outcome)
        session = store.add_session()
        store.commit_gate = asyncio.Event()
        registry = RunRegistry(store, factory, settings())
        run = await registry.start_run(session, "question")
        subscription = run.events.subscribe(0)
        await store.commit_entered.wait()
        assert run.status == "committing"
        assert not run.terminal
        assert not run.durable
        assert registry.active_run_id(session) == run.id
        assert all(event.type != f"run.{outcome}" for event in drain(subscription))
        with pytest.raises(SessionConflictError):
            await registry.start_run(session, "too soon")
        store.commit_gate.set()
        await complete(run)
        terminal = drain(subscription)
        assert [event.type for event in terminal] == [f"run.{outcome}"]
        assert terminal[0].data["durable"] is True
        assert run.status == outcome
        assert run.durable
        assert run.revision == 1
        assert registry.active_run_id(session) is None
        assert store.commands[0].status == outcome
        subscription.close()
        await registry.aclose()

    run_scenario(scenario)


@pytest.mark.parametrize("outcomes", [["after"], ["before", "ok"], ["before", "after"]])
def test_unknown_commit_acknowledgements_reconcile_without_duplicate_history(outcomes):
    async def scenario():
        store, factory = FakeStore(), FakeFactory()
        session = store.add_session()
        store.commit_outcomes.extend(outcomes)
        registry = RunRegistry(store, factory, settings())
        run = await registry.start_run(session, "question")
        await complete(run)
        assert run.status == "completed"
        assert run.durable
        assert not run.reconciliation_required
        assert len(store.histories[session]) == 2
        assert len(store.commands) == len(outcomes)
        assert all(command is store.commands[0] for command in store.commands)
        assert registry.active_run_id(session) is None
        await registry.aclose()

    run_scenario(scenario)


@pytest.mark.parametrize("failure", ["receipt_unavailable", "retry_unknown", "receipt_mismatch"])
def test_unconfirmed_persistence_holds_session_and_is_not_reported_as_success(failure):
    async def scenario():
        store, factory = FakeStore(), FakeFactory()
        session = store.add_session()
        if failure == "receipt_unavailable":
            store.commit_outcomes.extend(["after"])
            store.receipt_error = True
        elif failure == "receipt_mismatch":
            store.commit_outcomes.extend(["after"])
            store.corrupt_receipt = True
        else:
            store.commit_outcomes.extend(["before", "before"])
        now = [0.0]
        registry = RunRegistry(store, factory, settings(gateway_run_ttl_s=1), clock=lambda: now[0])
        run = await registry.start_run(session, "question")
        await complete(run)
        assert run.status == "failed"
        assert run.error == "storage_unconfirmed"
        assert not run.durable
        assert run.reconciliation_required
        assert registry.active_run_id(session) == run.id
        with pytest.raises(SessionConflictError):
            await registry.start_run(session, "unsafe continuation")
        with pytest.raises(SessionConflictError):
            await registry.delete_session(session)
        now[0] = 2.0
        assert registry.get_run(run.id) is run
        await registry.aclose()
        assert registry.active_run_id(session) is None

    run_scenario(scenario)


@pytest.mark.parametrize("phase", ["before_background_task", "during_history"])
def test_stop_before_agent_creation_does_not_generate_or_commit_a_turn(phase):
    async def scenario():
        store, factory = FakeStore(), FakeFactory()
        session = store.add_session()
        if phase == "during_history":
            store.load_gate = asyncio.Event()
        registry = RunRegistry(store, factory, settings())
        run = await registry.start_run(session, "question")
        if phase == "during_history":
            await store.load_entered.wait()
        assert registry.stop_run(run.id) is run
        event_id = run.events.latest_id
        registry.stop_run(run.id)
        assert run.events.latest_id == event_id
        if store.load_gate is not None:
            store.load_gate.set()
        await complete(run)
        assert run.status == "stopped"
        assert not run.durable
        assert store.commands == []
        assert factory.agents == []
        assert registry.active_run_id(session) is None
        terminal_sequence = run.events.latest_id
        registry.stop_run(run.id)
        assert run.events.latest_id == terminal_sequence
        await registry.aclose()

    run_scenario(scenario)


def test_cooperative_stop_persists_partial_output_and_next_run_restores_it():
    async def scenario():
        store, factory = FakeStore(), FakeFactory(blocked=True)
        session = store.add_session()
        registry = RunRegistry(store, factory, settings())
        first = await registry.start_run(session, "first")
        await store.load_entered.wait()
        registry.stop_run(first.id)
        await complete(first)
        assert first.status == "stopped"
        assert first.durable
        previous = store.histories[session]
        assert previous[-1]["content"] == "partial answer"
        second = await registry.start_run(session, "second")
        await asyncio.sleep(0)
        agent = factory.agents[-1]
        assert agent.history == previous
        agent.release.set()
        await complete(second)
        assert len(store.histories[session]) == 4
        assert store.commands[1].expected_count == 2
        await registry.aclose()

    run_scenario(scenario)


@pytest.mark.parametrize("cooperative", [True, False])
def test_shutdown_drains_cooperative_agents_and_cancels_unresponsive_ones(cooperative):
    async def scenario():
        store, factory = FakeStore(), FakeFactory(blocked=True, cooperative=cooperative)
        session = store.add_session()
        registry = RunRegistry(store, factory, settings())
        run = await registry.start_run(session, "question")
        await store.load_entered.wait()
        agent = factory.agents[0]
        subscription = run.events.subscribe(0)
        await registry.aclose()
        assert run.terminal
        assert run.task is None
        assert not run.events.closed
        assert drain(subscription)[-1].type == ("run.stopped" if cooperative else "run.failed")
        subscription.close()
        assert agent.closed
        assert agent.cancelled is not cooperative
        assert run.durable is cooperative
        assert run.status == ("stopped" if cooperative else "failed")
        assert registry.active_run_id(session) is None
        assert len(store.commands) == int(cooperative)
        with pytest.raises(GatewayClosingError):
            await registry.start_run(session, "too late")
        await registry.aclose()

    run_scenario(scenario)


def test_shutdown_cancel_during_commit_preserves_uncertain_state():
    async def scenario():
        store, factory = FakeStore(), FakeFactory()
        session = store.add_session()
        store.commit_gate = asyncio.Event()
        registry = RunRegistry(store, factory, settings())
        run = await registry.start_run(session, "question")
        await store.commit_entered.wait()
        await registry.aclose()
        assert run.status == "failed"
        assert run.error == "shutdown_cancelled"
        assert run.reconciliation_required
        assert not run.durable
        assert run.task is None
        assert factory.agents[0].closed

    run_scenario(scenario)


def test_failed_admission_and_agent_exception_release_resources_for_retry():
    async def scenario():
        store, factory = FakeStore(), FakeFactory(blocked=True)
        session = store.add_session()
        registry = RunRegistry(store, factory, settings(gateway_max_active_runs=1))
        with pytest.raises(SessionNotFoundError):
            await registry.start_run("f" * 32, "missing")
        assert registry.active_run_id("f" * 32) is None
        run = await registry.start_run(session, "question")
        await store.load_entered.wait()
        agent = factory.agents[0]
        agent.failure = RuntimeError("private provider body")
        agent.release.set()
        await complete(run)
        assert run.error == "run_failed"
        assert "private provider body" not in str(run.snapshot())
        assert not run.durable
        assert agent.closed
        assert registry.active_run_id(session) is None
        assert store.commands == []
        await registry.aclose()

    run_scenario(scenario)


def test_retention_ttl_and_capacity_evict_completed_runs_but_preserve_active_runs():
    async def scenario():
        store, factory = FakeStore(), FakeFactory()
        session = store.add_session()
        now = [0.0]
        registry = RunRegistry(
            store,
            factory,
            settings(gateway_max_runs=2, gateway_max_active_runs=2, gateway_run_ttl_s=10),
            clock=lambda: now[0],
        )
        first = await registry.start_run(session, "first")
        await complete(first)
        factory.options["blocked"] = True
        active = await registry.start_run(store.add_session(), "active")
        await asyncio.sleep(0)
        third = await registry.start_run(session, "third")
        assert first.events.closed
        with pytest.raises(RunNotFoundError):
            registry.get_run(first.id)
        assert registry.get_run(active.id) is active
        registry.stop_run(third.id)
        await complete(third)
        now[0] = 11.0
        with pytest.raises(RunNotFoundError):
            registry.get_run(third.id)
        assert registry.get_run(active.id) is active
        assert not active.events.closed
        await registry.aclose()

    run_scenario(scenario)


def test_deleting_completed_session_clears_history_and_retained_events():
    async def scenario():
        store, factory = FakeStore(), FakeFactory()
        session = store.add_session()
        registry = RunRegistry(store, factory, settings())
        run = await registry.start_run(session, "question")
        await complete(run)
        await registry.delete_session(session)
        assert session not in store.sessions
        assert session not in store.histories
        assert run.events.closed
        with pytest.raises(RunNotFoundError):
            registry.get_run(run.id)
        await registry.aclose()

    run_scenario(scenario)


def test_snapshot_retry_resets_text_and_output_limit_bounds_retained_display():
    async def scenario():
        store, factory = FakeStore(), FakeFactory(blocked=True)
        registry = RunRegistry(store, factory, settings())
        run = await registry.start_run(store.add_session(), "question")
        await store.load_entered.wait()
        agent = factory.agents[0]
        agent.callback(TextDelta("discarded attempt"))
        agent.callback(RetryScheduled(1, 3, 0.1, "timeout"))
        assert run.snapshot()["text"] == ""
        assert not run.text_truncated
        agent.callback(TextDelta("new attempt"))
        snapshot = run.snapshot()
        assert snapshot["text"] == "new attempt"
        assert snapshot["last_event_id"] == run.events.latest_id
        snapshot["text"] = "external mutation"
        assert run.text == "new attempt"
        agent.callback(TextDelta("中" * (MAX_SNAPSHOT_CHARS + 1)))
        assert len(run.text) == MAX_SNAPSHOT_CHARS
        assert run.text_truncated
        assert "Gateway output limit reached" in agent.interrupts
        replay = run.events.subscribe(0)
        events = drain(replay)
        assert any(event.type == "retry.scheduled" for event in events)
        assert all(
            len(event.data["text"]) <= 4096 for event in events if event.type == "text.delta"
        )
        replay.close()
        await complete(run)
        await registry.aclose()

    run_scenario(scenario)
