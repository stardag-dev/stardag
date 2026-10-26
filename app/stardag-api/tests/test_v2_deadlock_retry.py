"""A v2 write that Postgres aborts as a deadlock victim is re-run, not a 500
(``stardag_api.services.tx.retry_on_deadlock``).

The deadlock is real, between two Postgres transactions, with the route on
its ordinary lock order: ``exit-early`` locks the build row, then inserts
its event. A second transaction holds the ``event`` table in ``SHARE`` mode
(which an insert's ``ROW EXCLUSIVE`` waits for), waits until the route is
blocked on it, then asks for the build row ``FOR UPDATE``. Postgres aborts
one of the two -- usually the route, which began waiting first; a run where
it picks the other side is discarded and the deadlock arranged again.

Both waits are asserted from a third connection (``pg_blocking_pids``)
before anything is concluded, so the test cannot pass on an arrangement
that never formed a cycle; and the control shows the same arrangement is a
deadlock error when retries are switched off.
"""

from __future__ import annotations

import ast
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from stardag_api.auth import get_api_key_auth, get_sdk_auth
from stardag_api.auth.tokens import InternalTokenPayload
from stardag_api.models import ApiKey
from stardag_api.services import tx
from stardag_api.services.api_keys import create_api_key
from stardag_api.services.errors import RecordedConflict
from tests.conftest import (
    DEFAULT_ENVIRONMENT_ID,
    DEFAULT_USER_ID,
    DEFAULT_WORKSPACE_ID,
)

#: How long to wait for a lock wait to show up in ``pg_stat_activity``.
_WAIT_FOR_LOCK_SECONDS = 10.0

#: How many times to arrange the deadlock before giving up on Postgres
#: choosing the route as its victim (see :class:`_Outcome`).
_ARRANGEMENTS = 5


async def _poll(conn: AsyncConnection, sql: str, **params: object) -> object:
    """The first non-NULL scalar ``sql`` returns, polled until it does."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WAIT_FOR_LOCK_SECONDS
    while loop.time() < deadline:
        value = await conn.scalar(text(sql), params)
        if value is not None:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for: {sql} {params}")


async def _create_build(client: AsyncClient) -> UUID:
    response = await client.post("/api/v2/builds", json={"root_task_ids": ["a" * 64]})
    assert response.status_code == 200, response.text
    return UUID(response.json()["id"])


@dataclass
class _Outcome:
    #: Whether Postgres chose the route as the victim. It aborts the side
    #: whose own deadlock check finds the cycle, which is usually -- not
    #: always -- the first waiter; a run where it chose the other side says
    #: nothing about the retry, and the caller arranges the deadlock again.
    route_was_victim: bool
    #: The route's response, or what it raised.
    result: Response | BaseException


async def _deadlock_exit_early(
    client: AsyncClient, engine: AsyncEngine, build_id: UUID
) -> _Outcome:
    """Run ``exit-early`` into a deadlock, once both waits are observed."""
    async with (
        engine.connect() as other,
        engine.connect() as observer,
    ):
        await observer.execution_options(isolation_level="AUTOCOMMIT")
        await other.begin()
        other_pid = await other.scalar(text("SELECT pg_backend_pid()"))
        await other.execute(text("LOCK TABLE event IN SHARE MODE"))

        route = asyncio.create_task(
            client.post(f"/api/v2/builds/{build_id}/exit-early")
        )
        # The route holds the build row and waits on ``other``'s table lock.
        route_pid = await _poll(
            observer,
            "SELECT pid FROM pg_stat_activity"
            " WHERE CAST(:other AS int) = ANY(pg_blocking_pids(pid))",
            other=other_pid,
        )
        assert not route.done()

        # Close the cycle: ``other`` waits on the build row the route holds.
        lock_build_row = asyncio.create_task(
            other.execute(
                text("SELECT id FROM build WHERE id = :id FOR UPDATE"),
                {"id": build_id},
            )
        )
        await _poll(
            observer,
            "SELECT 1 WHERE CAST(:route AS int)"
            " = ANY(pg_blocking_pids(CAST(:other AS int)))",
            route=route_pid,
            other=other_pid,
        )
        # Postgres aborts one side, which frees what it held. If that was
        # the route, ``other`` gets the build row and finishes, so a retry
        # can proceed; if it was ``other``, the route simply goes on.
        try:
            await lock_build_row
        except DBAPIError as exc:
            assert tx.is_deadlock(exc), exc
            await other.rollback()
            route_was_victim = False
        else:
            await other.commit()
            route_was_victim = True
        (result,) = await asyncio.gather(route, return_exceptions=True)
        return _Outcome(route_was_victim, result)


async def _deadlock_with_route_as_victim(
    client: AsyncClient, engine: AsyncEngine
) -> tuple[UUID, Response | BaseException]:
    for _ in range(_ARRANGEMENTS):
        build_id = await _create_build(client)
        outcome = await _deadlock_exit_early(client, engine, build_id)
        if outcome.route_was_victim:
            return build_id, outcome.result
    raise AssertionError(f"the route was never the victim in {_ARRANGEMENTS} runs")


async def test_a_deadlocked_write_is_retried_to_a_2xx(
    client: AsyncClient,
    async_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
):
    with caplog.at_level(logging.WARNING, logger=tx.logger.name):
        build_id, response = await _deadlock_with_route_as_victim(client, async_engine)
        retries = [r.getMessage() for r in caplog.records if r.name == tx.logger.name]

    assert isinstance(response, Response), response
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "exit_early"
    # One retry, in the run where the route was the victim (a run where it
    # was not logs none).
    assert len(retries) == 1, retries
    assert "deadlock detected in exit_early" in retries[0]

    # The aborted attempt left nothing behind: one exit-early event.
    events = await client.get(f"/api/v2/builds/{build_id}/events")
    assert [e["event_type"] for e in events.json()["events"]].count(
        "build_exit_early"
    ) == 1


async def test_without_retries_the_same_arrangement_is_a_deadlock(
    client: AsyncClient,
    async_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
):
    """The control: the arrangement above really is a deadlock, and the
    retry is what turns it into a 2xx."""
    monkeypatch.setattr(tx, "MAX_ATTEMPTS", 1)
    _, result = await _deadlock_with_route_as_victim(client, async_engine)
    assert isinstance(result, DBAPIError), result
    assert tx.is_deadlock(result)


class _DeadlockOrig(Exception):
    sqlstate = tx.DEADLOCK_DETECTED


def _deadlock() -> DBAPIError:
    """What SQLAlchemy raises for a deadlock victim, without the deadlock."""
    return DBAPIError("SELECT 1", None, _DeadlockOrig())


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tx, "BACKOFF_BASE_SECONDS", 0)


async def test_a_deadlock_after_a_commit_is_not_retried(async_session):
    """A call that has committed one of its transactions is not re-run:
    that would repeat the committed part."""
    calls = 0

    @tx.retry_on_deadlock
    async def two_transactions(session) -> None:
        nonlocal calls
        calls += 1
        async with tx.transaction(session):
            await session.execute(text("SELECT 1"))
        async with tx.transaction(session):
            raise _deadlock()

    with pytest.raises(DBAPIError):
        await two_transactions(async_session)
    assert calls == 1


async def test_a_deadlock_before_any_commit_is_retried(async_session, no_backoff):
    calls = 0

    @tx.retry_on_deadlock
    async def flaky(session) -> int:
        nonlocal calls
        calls += 1
        async with tx.transaction(session):
            if calls < tx.MAX_ATTEMPTS:
                raise _deadlock()
            return calls

    assert await flaky(async_session) == tx.MAX_ATTEMPTS


async def test_the_session_may_be_passed_by_keyword(async_session, no_backoff):
    calls = 0

    @tx.retry_on_deadlock
    async def flaky(session) -> int:
        nonlocal calls
        calls += 1
        async with tx.transaction(session):
            if calls == 1:
                raise _deadlock()
            return calls

    assert await flaky(session=async_session) == 2


async def test_a_nested_call_is_retried_by_its_outer_call(async_session, no_backoff):
    """The inner call passes a deadlock through; the outer call, which owns
    the transaction, is the one re-run."""
    outer_calls = inner_calls = 0

    @tx.retry_on_deadlock
    async def inner(session) -> None:
        nonlocal inner_calls
        inner_calls += 1
        if inner_calls == 1:
            raise _deadlock()

    @tx.retry_on_deadlock
    async def outer(session) -> None:
        nonlocal outer_calls
        outer_calls += 1
        async with tx.transaction(session):
            await inner(session)

    await outer(async_session)
    assert (outer_calls, inner_calls) == (2, 2)


async def test_a_deadlock_committing_a_recorded_refusal_is_retried(
    async_session, monkeypatch, no_backoff
):
    """``transaction`` commits a recorded refusal from its ``except`` clause,
    so a deadlock there escapes without its rollback; the wrapper rolls the
    session back itself before the next attempt, which records the refusal
    and raises it."""
    commit = async_session.commit
    rollback = async_session.rollback
    commits = rollbacks = 0

    async def commit_deadlocking_once() -> None:
        nonlocal commits
        commits += 1
        if commits == 1:
            raise _deadlock()
        await commit()

    async def counting_rollback() -> None:
        nonlocal rollbacks
        rollbacks += 1
        await rollback()

    monkeypatch.setattr(async_session, "commit", commit_deadlocking_once)
    monkeypatch.setattr(async_session, "rollback", counting_rollback)
    calls = 0

    @tx.retry_on_deadlock
    async def refuse(session) -> None:
        nonlocal calls
        calls += 1
        async with tx.transaction(session):
            await session.execute(text("SELECT 1"))
            raise RecordedConflict("refused", "recorded, then refused")

    with pytest.raises(RecordedConflict):
        await refuse(async_session)
    assert (calls, commits) == (2, 2)
    assert rollbacks == 1  # the wrapper's own; transaction() never got to it


# ---------------------------------------------------------------------------
# The request's auth context survives a retry
# ---------------------------------------------------------------------------


@tx.retry_on_deadlock
async def _deadlocking_once(session, state: dict[str, int]) -> None:
    state["calls"] = state.get("calls", 0) + 1
    async with tx.transaction(session):
        await session.execute(text("SELECT 1"))
        if state["calls"] == 1:
            raise _deadlock()


async def test_api_key_auth_is_readable_after_a_retried_call(
    session_factory, no_backoff
):
    """The auth dependency loads ``ApiKey`` and ``Environment`` into the
    request session the service shares; the retry's rollback must not
    expire them (a later read would lazy-load: ``MissingGreenlet``). The
    key's ``last_used_at`` is committed on its own, so it survives too."""
    async with session_factory() as setup:
        key, full_key = await create_api_key(setup, DEFAULT_ENVIRONMENT_ID, "k")
        await setup.commit()

    async with session_factory() as db:
        api_key_auth = await get_api_key_auth(db, full_key)
        assert api_key_auth is not None
        auth = await get_sdk_auth(db, api_key_auth, None)
        assert auth is not None
        state: dict[str, int] = {}
        await _deadlocking_once(db, state)
        assert state["calls"] == 2
        assert auth.environment_id == DEFAULT_ENVIRONMENT_ID
        assert auth.workspace_id == DEFAULT_WORKSPACE_ID
        assert auth.api_key is not None and auth.api_key.id == key.id

    async with session_factory() as check:
        stored = await check.get(ApiKey, key.id)
        assert stored is not None and stored.last_used_at is not None


async def test_token_auth_is_readable_after_a_retried_call(session_factory, no_backoff):
    token = InternalTokenPayload(
        sub=str(DEFAULT_USER_ID),
        workspace_id=str(DEFAULT_WORKSPACE_ID),
        iss="test",
        aud="test",
        exp=0,
        iat=0,
    )
    async with session_factory() as db:
        auth = await get_sdk_auth(db, None, token, str(DEFAULT_ENVIRONMENT_ID))
        assert auth is not None
        await _deadlocking_once(db, {})
        assert auth.environment_id == DEFAULT_ENVIRONMENT_ID
        assert auth.user is not None
        assert (auth.user.id, auth.user.external_id) == (
            DEFAULT_USER_ID,
            "default-local-user",
        )


# ---------------------------------------------------------------------------
# Every service transaction is covered
# ---------------------------------------------------------------------------

_SERVICES = Path(tx.__file__).parent

#: Service functions that open a transaction but are deliberately not
#: retried, each with its reason. Empty: every one is safe to re-run.
_NOT_RETRIED: dict[str, str] = {}

#: Service modules that manage their own transactions (not the v2
#: registry; nothing in them is behind ``transaction``).
_OWN_TRANSACTIONS = {
    "local_auth": "local username/password auth, not a v2 registry service",
}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    return getattr(func, "id", None) or getattr(func, "attr", None)


def _calls(fn: ast.AST, names: set[str]) -> bool:
    return any(
        isinstance(node, ast.Call) and _call_name(node) in names
        for node in ast.walk(fn)
    )


def _decorator_names(fn: ast.AsyncFunctionDef) -> set[str]:
    return {getattr(d, "id", None) or getattr(d, "attr", "") for d in fn.decorator_list}


def _service_functions():
    """Every ``async def`` in the service modules, methods and nested
    functions included, as ``(qualified name, node)``."""
    for path in sorted(_SERVICES.glob("*.py")):
        if path.name == "tx.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.AsyncFunctionDef):
                yield path.stem, f"{path.stem}.{node.name}", node


def test_every_service_transaction_is_retried_on_deadlock():
    """A new writer gets the retry by the same rule as the existing ones,
    or an entry in ``_NOT_RETRIED`` saying why not.

    Only a call to ``transaction`` counts as opening one, so a raw
    ``commit()`` / ``begin()`` is refused outright (see the next test): the
    retry's commit detection sees ``transaction``'s commits only."""
    missing = [
        qualified
        for _, qualified, node in _service_functions()
        if _calls(node, {"transaction"})
        and qualified not in _NOT_RETRIED
        and "retry_on_deadlock" not in _decorator_names(node)
    ]
    assert not missing, (
        f"service functions opening a transaction without @retry_on_deadlock:"
        f" {missing} (decorate them once they are safe to re-run, see"
        f" services/tx.py, or list them in _NOT_RETRIED with the reason)"
    )


def test_services_open_transactions_only_through_transaction():
    """``begin_nested`` (a savepoint inside ``transaction``) is fine."""
    raw = [
        qualified
        for module, qualified, node in _service_functions()
        if module not in _OWN_TRANSACTIONS
        and _calls(node, {"commit", "begin", "rollback"})
    ]
    assert not raw, (
        f"service functions committing or beginning on their own: {raw};"
        f" open the transaction with services.tx.transaction"
    )
