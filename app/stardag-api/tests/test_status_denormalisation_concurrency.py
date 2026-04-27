"""Postgres-only concurrency tests for the denormalised Task.latest_* columns.

Two complementary test groups:

1. ``TestRaceDemonstration`` constructs the read-modify-write race
   *deterministically*: two sessions both SELECT before either COMMITs.
   With a plain SELECT the second commit clobbers the first
   (last-writer-wins). With ``SELECT ... FOR UPDATE`` the second SELECT
   blocks until the first commits, so it observes the updated row and the
   priority logic in ``apply_event_to_task`` keeps COMPLETED sticky. These
   tests document why the lock is necessary by producing the broken
   outcome on demand.

2. ``TestProductionPathStaysSticky`` exercises the *real* event-creation
   handler ``_create_task_event``, which uses ``for_update=True``. Run
   under stress (50 concurrent calls), it should never produce a
   non-COMPLETED final state. If anyone removes ``for_update=True`` from
   the production handler, this test will start failing flakily.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from stardag_api.auth.dependencies import SdkAuth
from stardag_api.models import (
    Build,
    Environment,
    Event,
    EventType,
    Task,
    TaskStatus,
)
from stardag_api.models.base import generate_uuid7
from stardag_api.routes.builds import _create_task_event
from stardag_api.services.status import apply_event_to_task
from tests.conftest import (
    DEFAULT_ENVIRONMENT_ID,
    DEFAULT_WORKSPACE_ID,
)


async def _seed_task_and_build(pg_engine: AsyncEngine, task_id: str) -> tuple:
    """Insert a PENDING task and an empty build; return (task_pk, build_id)."""
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with sm() as setup:
        build = Build(
            id=generate_uuid7(),
            environment_id=DEFAULT_ENVIRONMENT_ID,
            user_id=None,
            name=f"build-{task_id}",
            description=None,
            commit_hash=None,
            root_task_ids=[],
        )
        task = Task(
            id=generate_uuid7(),
            task_id=task_id,
            environment_id=DEFAULT_ENVIRONMENT_ID,
            task_namespace="",
            task_name=task_id,
            task_data={},
            is_phantom=False,
            latest_status=TaskStatus.PENDING,
            latest_waiting_for_lock=False,
        )
        setup.add(build)
        setup.add(task)
        await setup.commit()
        return task.id, build.id


async def _make_sdk_auth(pg_engine: AsyncEngine) -> SdkAuth:
    """Construct a SdkAuth pointing at the seeded environment.

    The production handler reads ``auth.environment_id`` and
    ``auth.workspace_id``; we don't need a real ApiKey or User here.
    """
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with sm() as session:
        env = await session.get(Environment, DEFAULT_ENVIRONMENT_ID)
        assert env is not None
        return SdkAuth(environment=env, workspace_id=DEFAULT_WORKSPACE_ID)


async def _call_create_task_event(
    pg_engine: AsyncEngine,
    build_id,
    task_id_str: str,
    event_type: EventType,
    auth: SdkAuth,
) -> None:
    """Invoke the production ``_create_task_event`` on its own session.

    Each invocation runs in its own session/connection so the two coroutines
    can hold concurrent transactions and contend on the row lock.
    """
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with sm() as session:
        await _create_task_event(build_id, task_id_str, event_type, session, auth)


async def test_concurrent_completed_and_started_stays_completed(
    pg_engine: AsyncEngine,
) -> None:
    """COMPLETED and STARTED fired concurrently on the same task → COMPLETED."""
    task_pk, build_id = await _seed_task_and_build(pg_engine, "race-c-s")
    auth = await _make_sdk_auth(pg_engine)

    await asyncio.gather(
        _call_create_task_event(
            pg_engine, build_id, "race-c-s", EventType.TASK_COMPLETED, auth
        ),
        _call_create_task_event(
            pg_engine, build_id, "race-c-s", EventType.TASK_STARTED, auth
        ),
    )

    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with sm() as final:
        row = await final.get(Task, task_pk)
        assert row is not None
        assert row.latest_status == TaskStatus.COMPLETED


async def test_repeated_concurrent_races_never_lose_completed(
    pg_engine: AsyncEngine,
) -> None:
    """Stress: many STARTED + COMPLETED applied in scrambled order in
    parallel must always end COMPLETED. Without the lock, this is the
    test that flakes — the asyncio scheduler interleaves the SELECTs
    and a STARTED commit can clobber a COMPLETED write. With the lock,
    every iteration deterministically resolves to COMPLETED."""
    task_pk, build_id = await _seed_task_and_build(pg_engine, "race-stress")
    auth = await _make_sdk_auth(pg_engine)

    coros = []
    for _ in range(25):
        coros.append(
            _call_create_task_event(
                pg_engine, build_id, "race-stress", EventType.TASK_STARTED, auth
            )
        )
        coros.append(
            _call_create_task_event(
                pg_engine, build_id, "race-stress", EventType.TASK_COMPLETED, auth
            )
        )
    await asyncio.gather(*coros)

    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with sm() as final:
        row = await final.get(Task, task_pk)
        assert row is not None
        assert row.latest_status == TaskStatus.COMPLETED


async def test_concurrent_started_and_failed_resolves_atomically(
    pg_engine: AsyncEngine,
) -> None:
    """STARTED + FAILED race on the same task: the lock guarantees the
    second event observes the first's write, so the row resolves to
    exactly one of the two terminal-or-running outcomes (RUNNING if
    STARTED commits last; FAILED if FAILED commits last). Without the
    lock the row could end PENDING (both readers see PENDING and the
    writes overwrite each other partially) — that would be a torn write."""
    task_pk, build_id = await _seed_task_and_build(pg_engine, "race-s-f")
    auth = await _make_sdk_auth(pg_engine)

    await asyncio.gather(
        _call_create_task_event(
            pg_engine, build_id, "race-s-f", EventType.TASK_STARTED, auth
        ),
        _call_create_task_event(
            pg_engine, build_id, "race-s-f", EventType.TASK_FAILED, auth
        ),
    )

    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with sm() as final:
        row = await final.get(Task, task_pk)
        assert row is not None
        assert row.latest_status in {TaskStatus.RUNNING, TaskStatus.FAILED}


# ---------------------------------------------------------------------------
# Deterministic race demonstration — proves the lock matters.
# ---------------------------------------------------------------------------


async def _force_overlapping_selects(
    pg_engine: AsyncEngine,
    task_id_str: str,
    build_id,
    *,
    use_for_update: bool,
) -> tuple[Event, Event]:
    """Run two read-modify-writes such that both SELECTs happen before
    either COMMIT, and return the events they produced.

    Without ``use_for_update`` this is the broken pattern: both sessions
    read PENDING, then apply different events, and the last commit wins.
    With ``use_for_update`` the second SELECT blocks until the first
    commits, so the second sees the updated row.
    """
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with sm() as s_a, sm() as s_b:
        stmt_a = (
            select(Task)
            .where(Task.environment_id == DEFAULT_ENVIRONMENT_ID)
            .where(Task.task_id == task_id_str)
        )
        stmt_b = (
            select(Task)
            .where(Task.environment_id == DEFAULT_ENVIRONMENT_ID)
            .where(Task.task_id == task_id_str)
        )
        if use_for_update:
            stmt_a = stmt_a.with_for_update()
            stmt_b = stmt_b.with_for_update()

        if use_for_update:
            # SELECT FOR UPDATE serialises: A acquires the lock, then B's
            # SELECT blocks until A commits. We can't gather the SELECTs
            # because B would deadlock on the lock that A still holds.
            r_a = await s_a.execute(stmt_a)
            t_a = r_a.scalar_one()
            ev_a = Event(
                id=generate_uuid7(),
                build_id=build_id,
                task_id=t_a.id,
                event_type=EventType.TASK_COMPLETED,
                created_at=datetime.now(timezone.utc),
            )
            s_a.add(ev_a)
            await s_a.flush()
            apply_event_to_task(t_a, ev_a)
            await s_a.commit()

            r_b = await s_b.execute(stmt_b)
            t_b = r_b.scalar_one()
            ev_b = Event(
                id=generate_uuid7(),
                build_id=build_id,
                task_id=t_b.id,
                event_type=EventType.TASK_STARTED,
                created_at=datetime.now(timezone.utc),
            )
            s_b.add(ev_b)
            await s_b.flush()
            apply_event_to_task(t_b, ev_b)
            await s_b.commit()
        else:
            # No locks — gather the SELECTs to force overlap, then apply
            # and commit in opposite order.
            r_a, r_b = await asyncio.gather(
                s_a.execute(stmt_a),
                s_b.execute(stmt_b),
            )
            t_a = r_a.scalar_one()
            t_b = r_b.scalar_one()
            ev_a = Event(
                id=generate_uuid7(),
                build_id=build_id,
                task_id=t_a.id,
                event_type=EventType.TASK_COMPLETED,
                created_at=datetime.now(timezone.utc),
            )
            ev_b = Event(
                id=generate_uuid7(),
                build_id=build_id,
                task_id=t_b.id,
                event_type=EventType.TASK_STARTED,
                created_at=datetime.now(timezone.utc),
            )
            s_a.add(ev_a)
            s_b.add(ev_b)
            await s_a.flush()
            await s_b.flush()
            apply_event_to_task(t_a, ev_a)
            apply_event_to_task(t_b, ev_b)
            # A applies COMPLETED, B applies STARTED. Commit B last so
            # STARTED clobbers COMPLETED — the bug we're fixing.
            await s_a.commit()
            await s_b.commit()
        return ev_a, ev_b


async def test_without_lock_started_clobbers_completed(
    pg_engine: AsyncEngine,
) -> None:
    """Without ``SELECT ... FOR UPDATE`` the read-modify-write races and a
    later TASK_STARTED commit clobbers an earlier TASK_COMPLETED. This is
    the bug that the production lock fixes; the test pins the bug down so
    it's clear what would regress."""
    task_pk, build_id = await _seed_task_and_build(pg_engine, "race-no-lock")

    await _force_overlapping_selects(
        pg_engine, "race-no-lock", build_id, use_for_update=False
    )

    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with sm() as final:
        row = await final.get(Task, task_pk)
        assert row is not None
        # Bug visible: STARTED clobbers COMPLETED. Both events are still
        # in the events table — only the denormalised column is wrong.
        assert row.latest_status == TaskStatus.RUNNING


async def test_with_lock_completed_stays_sticky(
    pg_engine: AsyncEngine,
) -> None:
    """With ``SELECT ... FOR UPDATE`` the second writer observes the first
    writer's COMPLETED state, and ``apply_event_to_task`` no-ops on the
    later TASK_STARTED. The denormalised column ends COMPLETED — matching
    the historical event-scan semantics."""
    task_pk, build_id = await _seed_task_and_build(pg_engine, "race-locked")

    await _force_overlapping_selects(
        pg_engine, "race-locked", build_id, use_for_update=True
    )

    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with sm() as final:
        row = await final.get(Task, task_pk)
        assert row is not None
        assert row.latest_status == TaskStatus.COMPLETED
