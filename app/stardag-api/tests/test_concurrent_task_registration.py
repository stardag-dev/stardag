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


async def test_bulk_register_keeps_the_lock_order_the_other_writers_use(
    pg_engine, pg_client
) -> None:
    """Lock what exists, then create what does not -- in that order.

    Every writer here takes those two steps, and the shared order is the
    only reason they do not wait on each other. ``register_task`` locks
    its own task and only then reaches ``_reconcile_dependency_edges`` to
    create missing upstreams; a bulk call that inserted first and locked
    afterwards would hold a row that one is waiting to create while
    waiting for a row it holds.

    Two transactions, opposite order, and Postgres breaks the tie by
    killing one of them -- an HTTP 500, which is the failure this module
    exists to rule out. So the order is pinned here rather than left to
    whoever next finds the post-insert lock tidier.

    The other writer is simulated rather than driven through the
    endpoint, because what has to be true is a property of the lock
    graph: hold the existing row, then ask for the new one.
    """
    existing = "already-here"
    fresh = "brand-new"

    maker = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with maker() as setup:
        await _insert_row_uncommitted(setup, existing)
        await setup.commit()

    build_id = await _new_build(pg_client)

    async with maker() as other_writer:
        # Step one of the shared order: hold the row that exists.
        held = await other_writer.execute(
            select(Task)
            .where(Task.environment_id == DEFAULT_ENVIRONMENT_ID)
            .where(Task.task_id == existing)
            .with_for_update()
        )
        assert held.scalar_one() is not None

        call = asyncio.create_task(
            pg_client.post(
                f"/api/v1/builds/{build_id}/tasks/bulk",
                json={"tasks": [_task(existing), _task(fresh)]},
            )
        )
        await asyncio.sleep(SETTLE_SECONDS)
        assert not call.done(), (
            "the bulk call did not wait for the held row, so it is not "
            "taking the lock before it inserts and this test is not "
            "exercising the ordering it exists to pin"
        )

        # Step two: create the row that does not exist. This must not
        # block -- the bulk call cannot have inserted it, because it is
        # still waiting for the lock above. If it did insert first, the
        # two are now waiting on each other and Postgres kills one.
        try:
            await asyncio.wait_for(
                _insert_row_uncommitted(other_writer, fresh),
                timeout=CALL_TIMEOUT_SECONDS,
            )
        except Exception as error:  # pragma: no cover - the failure path
            raise AssertionError(
                "creating a new row deadlocked against the bulk call, which "
                "means the bulk call inserted before taking its locks: it "
                f"holds what this writer needs and vice versa ({error!r})"
            ) from error

        await other_writer.commit()

        response = await asyncio.wait_for(call, timeout=CALL_TIMEOUT_SECONDS)

    assert response.status_code == 201, f"{response.status_code} {response.text}"


async def test_the_two_endpoints_create_new_tasks_in_one_order(
    pg_engine, pg_client
) -> None:
    """A dependency and its parent, both new, registered from both sides.

    The endpoints have to agree on the order they create rows in, or they
    wait on each other. Bulk creates every missing task in sorted
    ``task_id`` order; the single path used to create *its own* task first
    and reach the missing upstreams afterwards, through
    ``_reconcile_dependency_edges``. With a dependency whose id sorts
    before its parent's, that is a cycle -- bulk holding the dependency
    and waiting for the parent, the single path holding the parent and
    waiting for the dependency -- and Postgres resolves it by killing one,
    which is a 500 on a registration that did nothing wrong.

    **The middle row is what makes this deterministic.** Bulk inserts its
    rows in one statement, so the window between "holds the dependency"
    and "wants the parent" is microseconds wide and cannot be aimed at
    from outside. A third task whose id sorts between the two, held
    uncommitted by another transaction, parks bulk exactly there: it has
    the dependency and is waiting, and stays waiting until this test says
    otherwise. Everything after that is the real interleaving, driven
    through both real endpoints.
    """
    dep = "aaa-dependency"
    middle = "mmm-parks-the-bulk-call"
    parent = "zzz-parent"

    build_id = await _new_build(pg_client)
    other_build_id = await _new_build(pg_client)

    maker = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with maker() as blocker:
        await _insert_row_uncommitted(blocker, middle)

        # Parks with the dependency inserted and held, waiting on `middle`.
        bulk = asyncio.create_task(
            pg_client.post(
                f"/api/v1/builds/{build_id}/tasks/bulk",
                json={
                    "tasks": [
                        _task(dep),
                        _task(middle),
                        _task(parent, dependency_task_ids=[dep]),
                    ]
                },
            )
        )
        await asyncio.sleep(SETTLE_SECONDS)
        assert not bulk.done(), "the bulk call did not park on the held row"

        # The single path, for the parent, naming the same dependency. It
        # must not end up holding the parent while waiting for a
        # dependency the bulk call holds.
        single = asyncio.create_task(
            pg_client.post(
                f"/api/v1/builds/{other_build_id}/tasks",
                json=_task(parent, dependency_task_ids=[dep]),
            )
        )
        await asyncio.sleep(SETTLE_SECONDS)
        assert not single.done(), (
            "the single call finished before the bulk call was released, so "
            "the two never overlapped and this test would pass against the "
            "very ordering it exists to rule out"
        )

        # Release the parking row. Both calls are now free to finish --
        # unless they are waiting on each other.
        await blocker.commit()

        single_response = await asyncio.wait_for(single, timeout=CALL_TIMEOUT_SECONDS)
        bulk_response = await asyncio.wait_for(bulk, timeout=CALL_TIMEOUT_SECONDS)

    assert bulk_response.status_code == 201, (
        f"bulk: {bulk_response.status_code} {bulk_response.text} -- a "
        "deadlock here means the two endpoints create rows in orders that "
        "disagree"
    )
    assert single_response.status_code == 201, (
        f"single: {single_response.status_code} {single_response.text} -- a "
        "deadlock here means the two endpoints create rows in orders that "
        "disagree"
    )

    async with maker() as check:
        for task_id in (dep, middle, parent):
            rows = await _rows_for(check, task_id)
            assert len(rows) == 1, f"{len(rows)} rows for {task_id}"


async def test_bulk_creates_an_omitted_upstream_with_its_batch_not_after_it(
    pg_engine, pg_client
) -> None:
    """The batch names a dependency it does not carry.

    Bulk used to create such an upstream in the reconcile step's safety
    hatch, *after* its batch was already inserted and held -- a second
    creation, later, in an order nothing else shares. That is the same
    cycle as the single path's, from the other side: bulk holding the
    parent and waiting for the dependency, the single path holding the
    dependency and waiting for the parent.

    The parking row sorts *after* the parent here, so bulk gets as far as
    inserting and holding the parent before it stops. That is what makes
    the old ordering reachable: with the batch in hand, the only thing
    left for it to create is the dependency somebody else now holds.
    """
    dep = "aaa-omitted-upstream"
    parent = "zzz-parent"
    park = "zzzz-parks-the-bulk-call"

    build_id = await _new_build(pg_client)
    other_build_id = await _new_build(pg_client)

    maker = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with maker() as blocker:
        await _insert_row_uncommitted(blocker, park)

        # The dependency is named but not carried by the batch.
        bulk = asyncio.create_task(
            pg_client.post(
                f"/api/v1/builds/{build_id}/tasks/bulk",
                json={
                    "tasks": [
                        _task(parent, dependency_task_ids=[dep]),
                        _task(park),
                    ]
                },
            )
        )
        await asyncio.sleep(SETTLE_SECONDS)
        assert not bulk.done(), "the bulk call did not park on the held row"

        single = asyncio.create_task(
            pg_client.post(
                f"/api/v1/builds/{other_build_id}/tasks",
                json=_task(parent, dependency_task_ids=[dep]),
            )
        )
        await asyncio.sleep(SETTLE_SECONDS)
        assert not single.done(), (
            "the single call finished before the bulk call was released, so "
            "the two never overlapped"
        )

        await blocker.commit()

        single_response = await asyncio.wait_for(single, timeout=CALL_TIMEOUT_SECONDS)
        bulk_response = await asyncio.wait_for(bulk, timeout=CALL_TIMEOUT_SECONDS)

    assert bulk_response.status_code == 201, (
        f"bulk: {bulk_response.status_code} {bulk_response.text} -- a "
        "deadlock here means the batch's omitted upstream is still being "
        "created after the batch rather than with it"
    )
    assert single_response.status_code == 201, (
        f"single: {single_response.status_code} {single_response.text}"
    )

    async with maker() as check:
        for task_id in (dep, parent, park):
            rows = await _rows_for(check, task_id)
            assert len(rows) == 1, f"{len(rows)} rows for {task_id}"


async def test_adding_dependencies_takes_the_downstream_before_it_creates(
    pg_engine, pg_client
) -> None:
    """The third writer, and the one whose cycle runs through a key lock.

    ``POST /tasks/{id}/dependencies`` creates missing upstreams as
    phantoms and then inserts the edges. The edge insert takes an implicit
    ``FOR KEY SHARE`` on the downstream row for its foreign key, and that
    conflicts with a ``FOR UPDATE`` a registration holds -- so this writer
    could end up holding a phantom while waiting on the downstream, while
    the registration held the downstream and waited to create that same
    phantom.

    Nothing about sorting prevents that: the lock it waits on is not a row
    it names, it is one the foreign key names for it. What prevents it is
    taking the downstream *first*, which is the order the registration
    endpoints already follow.

    The parking row sorts before the upstream, so the registration stops
    with the downstream held and the upstream not yet created -- which is
    precisely the window this writer used to walk into.
    """
    downstream = "ddd-downstream"
    park = "aaa-parks-the-registration"
    upstream = "xxx-upstream"

    build_id = await _new_build(pg_client)
    other_build_id = await _new_build(pg_client)

    # The downstream exists and is committed, so the registration below
    # locks it rather than creating it.
    maker = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with maker() as setup:
        await _insert_row_uncommitted(setup, downstream)
        await setup.commit()

    async with maker() as blocker:
        await _insert_row_uncommitted(blocker, park)

        registration = asyncio.create_task(
            pg_client.post(
                f"/api/v1/builds/{build_id}/tasks",
                json=_task(downstream, dependency_task_ids=[park, upstream]),
            )
        )
        await asyncio.sleep(SETTLE_SECONDS)
        assert not registration.done(), "the registration did not park on the held row"

        adding = asyncio.create_task(
            pg_client.post(
                f"/api/v1/builds/{other_build_id}/tasks/{downstream}/dependencies",
                json={"upstream_task_ids": [upstream], "is_dynamic": True},
            )
        )
        await asyncio.sleep(SETTLE_SECONDS)
        assert not adding.done(), (
            "the dependency call finished before the registration was "
            "released, so it never contended for the downstream and this "
            "test would pass against the very ordering it exists to rule "
            "out -- an implementation that creates the phantom first can "
            "finish early here and still satisfy the 200 below"
        )

        await blocker.commit()

        adding_response = await asyncio.wait_for(adding, timeout=CALL_TIMEOUT_SECONDS)
        registration_response = await asyncio.wait_for(
            registration, timeout=CALL_TIMEOUT_SECONDS
        )

    assert registration_response.status_code == 201, (
        f"registration: {registration_response.status_code} "
        f"{registration_response.text}"
    )
    assert adding_response.status_code == 200, (
        f"adding dependencies: {adding_response.status_code} "
        f"{adding_response.text} -- a deadlock here means this writer "
        "creates rows before taking the downstream it is about to "
        "reference"
    )

    async with maker() as check:
        rows = await _rows_for(check, upstream)
        assert len(rows) == 1, f"{len(rows)} rows for {upstream}"
