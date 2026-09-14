"""Postgres-only: registering a task somebody else is registering too.

Two builds that share a task register it at the same moment. That is not
an exotic case -- it is the case the execution claim exists for -- and it
used to end in a unique violation on ``uq_task_environment_taskid``, an
HTTP 500, and a build that died before it ever reached the claim.

**SQLite cannot evidence any of this**, which is why these live here: it
has no ``SELECT ... FOR UPDATE``, no ``ON CONFLICT ... constraint``, and
one writer at a time, so the window under test does not exist there. The
tier that found the bug (registry-live, two real Modal builds against a
deployed registry) can evidence it but only by luck -- the window is
milliseconds wide and it took weeks of runs to hit.

So the race is *arranged* rather than waited for: a second transaction
inserts the row and holds it uncommitted, which puts the endpoint exactly
in the position a concurrent registration puts it in, on demand and every
time.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from stardag_api.models import Event, Task
from stardag_api.models.base import generate_uuid7, utc_now
from stardag_api.models.event import EventType
from stardag_api.models.task import TaskStatus
from tests.conftest import DEFAULT_ENVIRONMENT_ID

pytestmark = pytest.mark.asyncio

SHARED = "shared-task"

# How long to give the blocked call before concluding it is really
# blocked. Only ever asserted in the direction load cannot fake: a slow
# machine can make the call take *longer*, never make it finish early, so
# `not done()` cannot become a false failure. If the call has finished by
# now it did not wait for the lock at all, which is the bug.
SETTLE_SECONDS = 1.0

# A blocked call that never unblocks is a hang, and a hang in CI is a
# thirty-minute timeout with nothing to read. Every await on the endpoint
# is bounded so the failure is an error message instead.
CALL_TIMEOUT_SECONDS = 30.0


def _task(task_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_namespace": "",
        "task_name": task_id,
        "task_data": {},
        **extra,
    }


async def _insert_row_uncommitted(session, task_id: str) -> None:
    """Create the row the endpoint is about to try to create, and hold it.

    Deliberately the raw insert a *different* caller would do, not a
    helper shared with the endpoint: the point is to occupy the row from
    outside, the way another process does.
    """
    await session.execute(
        pg_insert(Task).values(
            id=generate_uuid7(),
            task_id=task_id,
            environment_id=DEFAULT_ENVIRONMENT_ID,
            task_namespace="",
            task_name=task_id,
            task_data={"written_by": "the other caller"},
            is_phantom=False,
            created_at=utc_now(),
            latest_status=TaskStatus.PENDING,
            latest_waiting_for_lock=False,
        )
    )


async def _new_build(pg_client) -> str:
    response = await pg_client.post("/api/v1/builds", json={})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _rows_for(session, task_id: str) -> list[Task]:
    result = await session.execute(
        select(Task)
        .where(Task.environment_id == DEFAULT_ENVIRONMENT_ID)
        .where(Task.task_id == task_id)
    )
    return list(result.scalars().all())


async def _event_types_for(session, task_pk) -> list[str]:
    result = await session.execute(
        select(Event.event_type).where(Event.task_id == task_pk)
    )
    return [str(row) for row in result.scalars().all()]


async def _run_against_a_held_row(
    pg_engine,
    pg_client,
    *,
    url: str,
    payload: dict[str, Any],
    task_id: str,
    outcome: str,
):
    """Call ``url`` while another transaction holds ``task_id`` uncommitted.

    ``outcome`` is what that other transaction does in the end -- "commit"
    or "rollback". Both are real: a racing registration usually commits,
    and occasionally the caller fails afterwards and takes its row with
    it. The second case is the one that makes ``ON CONFLICT DO NOTHING``
    insufficient on its own, since it takes no action for a conflict that
    then disappears.
    """
    maker = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with maker() as blocker:
        await _insert_row_uncommitted(blocker, task_id)

        call = asyncio.create_task(pg_client.post(url, json=payload))
        await asyncio.sleep(SETTLE_SECONDS)
        assert not call.done(), (
            "the call did not wait for the uncommitted row: it either never "
            "attempted the insert or is not going through the unique "
            "constraint at all, so this test is not exercising the race"
        )

        if outcome == "commit":
            await blocker.commit()
        else:
            await blocker.rollback()

        return await asyncio.wait_for(call, timeout=CALL_TIMEOUT_SECONDS)


async def test_bulk_register_survives_a_concurrent_creator(
    pg_engine, pg_client
) -> None:
    """The exact failure: a 500 and a dead build, for a shared task."""
    build_id = await _new_build(pg_client)

    response = await _run_against_a_held_row(
        pg_engine,
        pg_client,
        url=f"/api/v1/builds/{build_id}/tasks/bulk",
        payload={"tasks": [_task(SHARED), _task("mine-alone")]},
        task_id=SHARED,
        outcome="commit",
    )

    assert response.status_code == 201, (
        "registering a task another build created a moment earlier must "
        f"succeed, not fail: {response.status_code} {response.text}"
    )

    maker = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with maker() as check:
        rows = await _rows_for(check, SHARED)
        assert len(rows) == 1, f"{len(rows)} rows for one task_id"
        # The other caller's row is the one that survives, untouched: this
        # call lost the race and a loser does not overwrite the winner.
        assert rows[0].task_data == {"written_by": "the other caller"}

        # And it is a *reference*, exactly as it would be for a row that
        # had been there for a week. The distinction the endpoint draws is
        # "did I create this", not "was it there when I looked".
        types = await _event_types_for(check, rows[0].id)
        assert types == [EventType.TASK_REFERENCED.value], types


async def test_bulk_register_survives_a_concurrent_creator_that_rolls_back(
    pg_engine, pg_client
) -> None:
    """The subtle half: the conflicting row goes away again.

    ``ON CONFLICT DO NOTHING`` does nothing for a conflict whose inserter
    then aborts -- the row is not there and this call did not insert it
    either. Handled by trying once more rather than by hoping, and without
    that this is a ``KeyError`` on a row the endpoint believes it has.
    """
    build_id = await _new_build(pg_client)

    response = await _run_against_a_held_row(
        pg_engine,
        pg_client,
        url=f"/api/v1/builds/{build_id}/tasks/bulk",
        payload={"tasks": [_task(SHARED)]},
        task_id=SHARED,
        outcome="rollback",
    )

    assert response.status_code == 201, (
        "the conflicting row was rolled back, so this call is the only "
        f"creator left and must create it: {response.status_code} "
        f"{response.text}"
    )

    maker = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with maker() as check:
        rows = await _rows_for(check, SHARED)
        assert len(rows) == 1, f"{len(rows)} rows for one task_id"
        # This call created it, so the data is this call's and the event
        # says PENDING rather than REFERENCED.
        assert rows[0].task_data == {}
        types = await _event_types_for(check, rows[0].id)
        assert types == [EventType.TASK_PENDING.value], types


async def test_single_register_survives_a_concurrent_creator(
    pg_engine, pg_client
) -> None:
    """The single-task endpoint had the same hole, for the same reason.

    ``SELECT ... FOR UPDATE`` locks rows; where there is no row there is
    nothing to lock, so two callers both find the task absent and both
    insert it.
    """
    build_id = await _new_build(pg_client)

    response = await _run_against_a_held_row(
        pg_engine,
        pg_client,
        url=f"/api/v1/builds/{build_id}/tasks",
        payload=_task(SHARED),
        task_id=SHARED,
        outcome="commit",
    )

    assert response.status_code == 201, f"{response.status_code} {response.text}"

    maker = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with maker() as check:
        rows = await _rows_for(check, SHARED)
        assert len(rows) == 1, f"{len(rows)} rows for one task_id"
        assert rows[0].task_data == {"written_by": "the other caller"}
        types = await _event_types_for(check, rows[0].id)
        assert types == [EventType.TASK_REFERENCED.value], types


async def test_single_register_survives_a_concurrent_creator_that_rolls_back(
    pg_engine, pg_client
) -> None:
    """Same aborted-conflict case, on the single-task path."""
    build_id = await _new_build(pg_client)

    response = await _run_against_a_held_row(
        pg_engine,
        pg_client,
        url=f"/api/v1/builds/{build_id}/tasks",
        payload=_task(SHARED),
        task_id=SHARED,
        outcome="rollback",
    )

    assert response.status_code == 201, (
        "the conflicting row was rolled back, so this call is the only "
        f"creator left and must create it: {response.status_code} "
        f"{response.text}"
    )

    maker = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with maker() as check:
        rows = await _rows_for(check, SHARED)
        assert len(rows) == 1, f"{len(rows)} rows for one task_id"
        assert rows[0].task_data == {}
        types = await _event_types_for(check, rows[0].id)
        assert types == [EventType.TASK_PENDING.value], types
