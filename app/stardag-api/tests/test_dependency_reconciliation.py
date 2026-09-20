"""Postgres-only integration tests for the batched dependency reconciliation
in routes/builds.py:_reconcile_dependency_edges.

These tests skip on SQLite because the implementation uses
sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_nothing(constraint=...)
to keep the path idempotent under concurrent registration, and the
idempotency it claims is per *structure scope*: the conflict target is
``(scope_key, upstream, downstream)``.

Every upstream an edge names must already be registered. That used to be
a placeholder row created on the fly; it is now a 400, because every
stardag build engine registers dependencies before the tasks that declare
them and a caller reaching here with an unknown id has a bug.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import Task, TaskDependency
from stardag_api.models.base import generate_uuid7
from stardag_api.routes.builds import _reconcile_dependency_edges
from tests.conftest import DEFAULT_ENVIRONMENT_ID


pytestmark = pytest.mark.asyncio

SCOPE = "code-rec:cfg-0"
OTHER_SCOPE = "code-rec:cfg-1"


async def _make_task(session: AsyncSession, task_id: str) -> Task:
    task = Task(
        id=generate_uuid7(),
        task_id=task_id,
        environment_id=DEFAULT_ENVIRONMENT_ID,
        task_namespace="",
        task_name=task_id,
        task_data={},
    )
    session.add(task)
    await session.flush()
    return task


async def _edges_into(
    session: AsyncSession, downstream_pk: UUID
) -> list[TaskDependency]:
    result = await session.execute(
        select(TaskDependency).where(TaskDependency.downstream_task_id == downstream_pk)
    )
    return list(result.scalars().all())


async def _reconcile(
    session: AsyncSession,
    downstream: Task,
    upstream_ids: list[str],
    *,
    scope_key: str = SCOPE,
) -> int:
    return await _reconcile_dependency_edges(
        db=session,
        environment_id=DEFAULT_ENVIRONMENT_ID,
        scope_key=scope_key,
        downstream_task_pk=downstream.id,
        downstream_task_id=downstream.task_id,
        upstream_task_ids=upstream_ids,
        is_dynamic=False,
    )


async def test_creates_edges_for_registered_upstreams_in_one_call(
    pg_session: AsyncSession,
) -> None:
    """Every edge lands in the scope it was reconciled under."""
    for tid in ("dep-1", "dep-2", "dep-3"):
        await _make_task(pg_session, tid)
    downstream = await _make_task(pg_session, "downstream-1")
    await pg_session.commit()

    inserted = await _reconcile(pg_session, downstream, ["dep-1", "dep-2", "dep-3"])
    await pg_session.commit()

    assert inserted == 3
    edges = await _edges_into(pg_session, downstream.id)
    assert len(edges) == 3
    assert {e.scope_key for e in edges} == {SCOPE}


async def test_unknown_upstream_is_refused_and_writes_nothing(
    pg_session: AsyncSession,
) -> None:
    """One unknown id refuses the whole call — the known ones are not
    written either, so a half-declared set never exists."""
    await _make_task(pg_session, "real-up")
    downstream = await _make_task(pg_session, "downstream-mixed")
    await pg_session.commit()
    # Read before the rollback below expires the instance.
    downstream_pk = downstream.id

    with pytest.raises(HTTPException) as excinfo:
        await _reconcile(pg_session, downstream, ["real-up", "missing-up"])
    assert excinfo.value.status_code == 400
    # ``cast`` rather than ``isinstance``: Starlette annotates ``detail`` as
    # ``str``, and narrowing a ``str`` to a ``dict`` leaves pyright with a
    # type that still indexes like a string.
    detail = cast(dict, excinfo.value.detail)
    assert isinstance(detail, dict)
    assert detail["error_code"] == "unknown_upstream_task_ids"
    assert detail["task_id"] == "downstream-mixed"
    assert detail["unknown_upstream_task_ids"] == ["missing-up"]

    await pg_session.rollback()
    assert await _edges_into(pg_session, downstream_pk) == []
    # And nothing was created for the unknown id.
    result = await pg_session.execute(
        select(Task).where(
            Task.environment_id == DEFAULT_ENVIRONMENT_ID,
            Task.task_id == "missing-up",
        )
    )
    assert result.scalar_one_or_none() is None


async def test_idempotent_within_a_scope_and_distinct_across_scopes(
    pg_session: AsyncSession,
) -> None:
    """The conflict target is (scope, upstream, downstream): repeating a
    call in the same scope inserts nothing, the same edge under another
    scope is a new row — another code version's fact, kept apart."""
    for tid in ("dep-a", "dep-b"):
        await _make_task(pg_session, tid)
    downstream = await _make_task(pg_session, "downstream-idem")
    await pg_session.commit()

    upstream_ids = ["dep-a", "dep-b"]
    first = await _reconcile(pg_session, downstream, upstream_ids)
    await pg_session.commit()
    second = await _reconcile(pg_session, downstream, upstream_ids)
    await pg_session.commit()

    assert first == 2
    assert second == 0
    assert len(await _edges_into(pg_session, downstream.id)) == 2

    third = await _reconcile(
        pg_session, downstream, upstream_ids, scope_key=OTHER_SCOPE
    )
    await pg_session.commit()
    assert third == 2
    edges = await _edges_into(pg_session, downstream.id)
    assert len(edges) == 4
    assert sorted(e.scope_key or "" for e in edges) == sorted(
        [SCOPE, SCOPE, OTHER_SCOPE, OTHER_SCOPE]
    )


async def test_empty_upstream_list_is_a_noop(
    pg_session: AsyncSession,
) -> None:
    downstream = await _make_task(pg_session, "downstream-empty")
    await pg_session.commit()

    inserted = await _reconcile(pg_session, downstream, [])
    await pg_session.commit()

    assert inserted == 0
    assert await _edges_into(pg_session, downstream.id) == []


async def test_duplicate_ids_in_input_are_deduplicated(
    pg_session: AsyncSession,
) -> None:
    await _make_task(pg_session, "dup-up")
    downstream = await _make_task(pg_session, "downstream-dup")
    await pg_session.commit()

    inserted = await _reconcile(pg_session, downstream, ["dup-up", "dup-up", "dup-up"])
    await pg_session.commit()

    assert inserted == 1
    assert len(await _edges_into(pg_session, downstream.id)) == 1


async def test_concurrent_reconcile_overlapping_upstreams_resolves_safely(
    pg_engine,
) -> None:
    """Two transactions concurrently reconcile overlapping upstream ids on
    different downstream tasks in one scope. The ON CONFLICT DO NOTHING edge
    inserts must converge to exactly one edge per (upstream, downstream)
    pair, and the shared upstream rows are locked in one agreed order so the
    two never wait on each other.

    This locks in the function's headline claim of being concurrent-safe;
    the sequential idempotency tests above don't actually race."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    shared_upstreams = ["conc-up-1", "conc-up-2", "conc-up-3"]
    async with sm() as setup:
        for tid in shared_upstreams:
            await _make_task(setup, tid)
        d_a = await _make_task(setup, "conc-down-a")
        d_b = await _make_task(setup, "conc-down-b")
        await setup.commit()
        d_a_pk, d_b_pk = d_a.id, d_b.id

    async def reconcile(downstream_pk, downstream_task_id):
        async with sm() as s:
            await _reconcile_dependency_edges(
                db=s,
                environment_id=DEFAULT_ENVIRONMENT_ID,
                scope_key=SCOPE,
                downstream_task_pk=downstream_pk,
                downstream_task_id=downstream_task_id,
                upstream_task_ids=shared_upstreams,
                is_dynamic=False,
            )
            await s.commit()

    await asyncio.gather(
        reconcile(d_a_pk, "conc-down-a"), reconcile(d_b_pk, "conc-down-b")
    )

    async with sm() as final:
        # Still exactly three upstream rows: nothing was created on the way.
        result = await final.execute(
            select(Task).where(
                Task.environment_id == DEFAULT_ENVIRONMENT_ID,
                Task.task_id.in_(shared_upstreams),
            )
        )
        assert len(list(result.scalars().all())) == 3
        # Each downstream has exactly 3 edges (one per shared upstream).
        assert len(await _edges_into(final, d_a_pk)) == 3
        assert len(await _edges_into(final, d_b_pk)) == 3
