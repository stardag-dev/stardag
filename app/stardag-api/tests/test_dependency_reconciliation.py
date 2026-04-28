"""Postgres-only integration tests for the batched dependency reconciliation
in routes/builds.py:_reconcile_dependency_edges.

These tests skip on SQLite because the implementation uses
sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_nothing(constraint=...)
to keep the path idempotent under concurrent registration. That's the same
shape the production code already used; this test just covers the new
batching behaviour against a real Postgres.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import Task, TaskDependency
from stardag_api.models.base import generate_uuid7
from stardag_api.routes.builds import _reconcile_dependency_edges
from tests.conftest import DEFAULT_ENVIRONMENT_ID


pytestmark = pytest.mark.asyncio


async def _make_real_task(
    session: AsyncSession,
    task_id: str,
    *,
    is_phantom: bool = False,
) -> Task:
    task = Task(
        id=generate_uuid7(),
        task_id=task_id,
        environment_id=DEFAULT_ENVIRONMENT_ID,
        task_namespace="",
        task_name=task_id,
        task_data={},
        is_phantom=is_phantom,
    )
    session.add(task)
    await session.flush()
    return task


async def _count_edges_into(session: AsyncSession, downstream_pk: UUID) -> int:
    result = await session.execute(
        select(TaskDependency).where(TaskDependency.downstream_task_id == downstream_pk)
    )
    return len(result.scalars().all())


async def test_creates_phantoms_and_edges_in_one_call(
    pg_session: AsyncSession,
) -> None:
    downstream = await _make_real_task(pg_session, "downstream-1")
    await pg_session.commit()

    inserted = await _reconcile_dependency_edges(
        db=pg_session,
        environment_id=DEFAULT_ENVIRONMENT_ID,
        downstream_task_pk=downstream.id,
        upstream_task_ids=["dep-1", "dep-2", "dep-3"],
        is_dynamic=False,
    )
    await pg_session.commit()

    assert inserted == 3
    assert await _count_edges_into(pg_session, downstream.id) == 3

    # Phantoms exist for all three upstream ids.
    for tid in ("dep-1", "dep-2", "dep-3"):
        result = await pg_session.execute(
            select(Task).where(
                Task.environment_id == DEFAULT_ENVIRONMENT_ID,
                Task.task_id == tid,
            )
        )
        row = result.scalar_one()
        assert row.is_phantom is True


async def test_mixed_existing_and_missing_upstreams(
    pg_session: AsyncSession,
) -> None:
    # One upstream task already exists as a real (non-phantom) row.
    real_upstream = await _make_real_task(pg_session, "real-up")
    downstream = await _make_real_task(pg_session, "downstream-mixed")
    await pg_session.commit()

    inserted = await _reconcile_dependency_edges(
        db=pg_session,
        environment_id=DEFAULT_ENVIRONMENT_ID,
        downstream_task_pk=downstream.id,
        upstream_task_ids=["real-up", "missing-up"],
        is_dynamic=False,
    )
    await pg_session.commit()

    assert inserted == 2
    assert await _count_edges_into(pg_session, downstream.id) == 2

    # Real task wasn't downgraded to phantom.
    refreshed = await pg_session.get(Task, real_upstream.id)
    assert refreshed is not None
    assert refreshed.is_phantom is False

    # Missing upstream got a phantom row.
    result = await pg_session.execute(
        select(Task).where(
            Task.environment_id == DEFAULT_ENVIRONMENT_ID,
            Task.task_id == "missing-up",
        )
    )
    phantom = result.scalar_one()
    assert phantom.is_phantom is True


async def test_idempotent_under_repeated_calls(
    pg_session: AsyncSession,
) -> None:
    downstream = await _make_real_task(pg_session, "downstream-idem")
    await pg_session.commit()

    upstream_ids = ["dep-a", "dep-b"]
    first = await _reconcile_dependency_edges(
        db=pg_session,
        environment_id=DEFAULT_ENVIRONMENT_ID,
        downstream_task_pk=downstream.id,
        upstream_task_ids=upstream_ids,
        is_dynamic=False,
    )
    await pg_session.commit()

    # Second call with identical inputs must not create duplicate edges.
    second = await _reconcile_dependency_edges(
        db=pg_session,
        environment_id=DEFAULT_ENVIRONMENT_ID,
        downstream_task_pk=downstream.id,
        upstream_task_ids=upstream_ids,
        is_dynamic=True,  # different is_dynamic value: should NOT update
    )
    await pg_session.commit()

    assert first == 2
    assert second == 0
    assert await _count_edges_into(pg_session, downstream.id) == 2

    # First-write-wins semantic: original is_dynamic=False is retained.
    edges_result = await pg_session.execute(
        select(TaskDependency).where(TaskDependency.downstream_task_id == downstream.id)
    )
    edges = list(edges_result.scalars().all())
    assert all(edge.is_dynamic is False for edge in edges)


async def test_empty_upstream_list_is_a_noop(
    pg_session: AsyncSession,
) -> None:
    downstream = await _make_real_task(pg_session, "downstream-empty")
    await pg_session.commit()

    inserted = await _reconcile_dependency_edges(
        db=pg_session,
        environment_id=DEFAULT_ENVIRONMENT_ID,
        downstream_task_pk=downstream.id,
        upstream_task_ids=[],
        is_dynamic=False,
    )
    await pg_session.commit()
    assert inserted == 0
    assert await _count_edges_into(pg_session, downstream.id) == 0


async def test_duplicate_ids_in_input_are_deduplicated(
    pg_session: AsyncSession,
) -> None:
    downstream = await _make_real_task(pg_session, "downstream-dup")
    await pg_session.commit()

    inserted = await _reconcile_dependency_edges(
        db=pg_session,
        environment_id=DEFAULT_ENVIRONMENT_ID,
        downstream_task_pk=downstream.id,
        upstream_task_ids=["dup-up", "dup-up", "dup-up"],
        is_dynamic=False,
    )
    await pg_session.commit()

    # One unique upstream — one phantom, one edge.
    assert inserted == 1
    assert await _count_edges_into(pg_session, downstream.id) == 1
    result = await pg_session.execute(
        select(Task).where(
            Task.environment_id == DEFAULT_ENVIRONMENT_ID,
            Task.task_id == "dup-up",
        )
    )
    rows = list(result.scalars().all())
    assert len(rows) == 1


async def test_concurrent_reconcile_overlapping_upstreams_resolves_safely(
    pg_engine,
) -> None:
    """Two transactions concurrently reconcile overlapping missing upstream
    ids on different downstream tasks. Both ON CONFLICT DO NOTHING phantom
    inserts and ON CONFLICT DO NOTHING edge inserts must converge to the
    correct row count: one phantom per unique upstream id, exactly one
    edge per (upstream, downstream) pair.

    This locks in the function's headline claim of being concurrent-safe;
    the sequential idempotency tests above don't actually race."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    # Two distinct downstreams so the test exercises the case where both
    # transactions race to create the SAME phantom upstream rows.
    async with sm() as setup:
        d_a = Task(
            id=generate_uuid7(),
            task_id="conc-down-a",
            environment_id=DEFAULT_ENVIRONMENT_ID,
            task_namespace="",
            task_name="conc-down-a",
            task_data={},
            is_phantom=False,
        )
        d_b = Task(
            id=generate_uuid7(),
            task_id="conc-down-b",
            environment_id=DEFAULT_ENVIRONMENT_ID,
            task_namespace="",
            task_name="conc-down-b",
            task_data={},
            is_phantom=False,
        )
        setup.add(d_a)
        setup.add(d_b)
        await setup.commit()
        d_a_pk, d_b_pk = d_a.id, d_b.id

    shared_upstreams = ["conc-up-1", "conc-up-2", "conc-up-3"]

    async def reconcile(downstream_pk):
        async with sm() as s:
            await _reconcile_dependency_edges(
                db=s,
                environment_id=DEFAULT_ENVIRONMENT_ID,
                downstream_task_pk=downstream_pk,
                upstream_task_ids=shared_upstreams,
                is_dynamic=False,
            )
            await s.commit()

    await asyncio.gather(reconcile(d_a_pk), reconcile(d_b_pk))

    # Exactly 3 phantoms, regardless of which transaction won the inserts.
    async with sm() as final:
        ph_result = await final.execute(
            select(Task).where(
                Task.environment_id == DEFAULT_ENVIRONMENT_ID,
                Task.task_id.in_(shared_upstreams),
            )
        )
        phantoms = list(ph_result.scalars().all())
        assert len(phantoms) == 3
        assert all(p.is_phantom for p in phantoms)

        # Each downstream has exactly 3 edges (one per shared upstream).
        assert await _count_edges_into(final, d_a_pk) == 3
        assert await _count_edges_into(final, d_b_pk) == 3
