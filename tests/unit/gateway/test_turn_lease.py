import asyncio

import pytest

from mertina_agent.exceptions import SessionConflictError
from mertina_agent.gateway.turn_lease import Lease, SessionTurnLeaseRegistry


def test_same_session_conflicts_even_for_same_owner():
    registry = SessionTurnLeaseRegistry()
    lease = registry.acquire("session", "run")
    assert registry.active_owner("session") == "run"
    for owner in ("run", "other-run"):
        with pytest.raises(SessionConflictError, match="active operation"):
            registry.acquire("session", owner)
    lease.release()
    assert registry.active_owner("session") is None


def test_release_is_idempotent_and_stale_tokens_cannot_release_new_owner():
    registry = SessionTurnLeaseRegistry()
    old = registry.acquire("session", "old")
    old.release()
    current = registry.acquire("session", "current")
    old.release()
    assert old.released
    assert not current.released
    assert registry.active_owner("session") == "current"
    # A different token with identical fields still does not own the lease.
    impostor = Lease(registry, "session", "current")
    impostor.release()
    assert registry.active_owner("session") == "current"
    current.release()
    assert registry.active_owner("session") is None


def test_different_sessions_progress_independently():
    async def scenario():
        registry = SessionTurnLeaseRegistry()
        both_entered = asyncio.Event()
        entered: set[str] = set()

        async def run(session):
            lease = registry.acquire(session, f"run-{session}")
            try:
                entered.add(session)
                if len(entered) == 2:
                    both_entered.set()
                await both_entered.wait()
                assert registry.active_owner(session) == f"run-{session}"
            finally:
                lease.release()

        async with asyncio.timeout(1):
            await asyncio.gather(run("one"), run("two"))
        assert registry.active_owner("one") is None
        assert registry.active_owner("two") is None

    asyncio.run(scenario())


def test_cancellation_finally_releases_for_the_next_turn():
    async def scenario():
        registry = SessionTurnLeaseRegistry()
        entered = asyncio.Event()

        async def run():
            lease = registry.acquire("session", "cancelled")
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                lease.release()

        task = asyncio.create_task(run())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        next_turn = registry.acquire("session", "next")
        assert registry.active_owner("session") == "next"
        next_turn.release()

    asyncio.run(scenario())
