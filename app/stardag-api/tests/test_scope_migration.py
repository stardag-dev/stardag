"""Postgres-only: the data steps of the scope-keyed edges migration.

Three things the migration does to existing rows, each of which would be
a silent regression if it stopped happening:

- every build gets the synthetic ``build:<id>`` scope;
- legacy edges are copied into the scope of every RUNNING build holding
  their downstream task — without that, a reactive build in flight across
  the deploy would lose every gate at once and run downstream tasks early;
- phantom placeholder rows, and their edges, are deleted.

Taken back through ``downgrade`` / ``upgrade`` so the steps run over rows
seeded in the *pre-migration* shape, with raw SQL because the ORM models
already describe the post-migration schema.
"""

import asyncio
import os
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import DEFAULT_ENVIRONMENT_ID

# The revision below the scope migration. Named rather than ``-1`` so a later
# migration stacked on top cannot silently turn this test into a no-op.
_SCOPE_DOWN_REVISION = "c5f2a8b71d34"

BUILDS = "/api/v1/builds"


async def _insert_task(
    session: AsyncSession, task_id: str, *, is_phantom: bool = False
) -> str:
    pk = str(uuid4())
    await session.execute(
        text(
            "INSERT INTO tasks (id, task_id, environment_id, task_namespace, "
            "task_name, task_data, is_phantom, latest_status, "
            "latest_waiting_for_lock, created_at) VALUES (:pk, :tid, :env, '', "
            ":tid, '{}'::jsonb, :phantom, 'pending', false, now())"
        ),
        {
            "pk": pk,
            "tid": task_id,
            "env": str(DEFAULT_ENVIRONMENT_ID),
            "phantom": is_phantom,
        },
    )
    return pk


async def _insert_edge(
    session: AsyncSession, upstream_pk: str, downstream_pk: str
) -> None:
    await session.execute(
        text(
            "INSERT INTO task_dependencies (id, upstream_task_id, downstream_task_id, "
            "is_dynamic, created_at) VALUES (:pk, :up, :down, false, now())"
        ),
        {"pk": str(uuid4()), "up": upstream_pk, "down": downstream_pk},
    )


async def _insert_event(session: AsyncSession, build_id: str, task_pk: str) -> None:
    await session.execute(
        text(
            "INSERT INTO events (id, build_id, task_id, event_type, created_at) "
            "VALUES (:pk, :build, :task, 'task_pending', now())"
        ),
        {"pk": str(uuid4()), "build": build_id, "task": task_pk},
    )


async def _insert_build(session: AsyncSession, *, status: str) -> str:
    pk = str(uuid4())
    await session.execute(
        text(
            "INSERT INTO builds (id, environment_id, name, root_task_ids, "
            "last_active_at, latest_status, latest_is_resumed, created_at) "
            "VALUES (:pk, :env, :name, '[]'::jsonb, now(), :status, false, now())"
        ),
        {
            "pk": pk,
            "env": str(DEFAULT_ENVIRONMENT_ID),
            "name": f"b-{pk[:8]}",
            "status": status,
        },
    )
    return pk


async def _scalar(session: AsyncSession, sql: str, **params):
    return (await session.execute(text(sql), params)).scalar()


@pytest.mark.asyncio
async def test_migration_scopes_running_builds_and_drops_phantoms(
    pg_client: AsyncClient, pg_session: AsyncSession, pg_engine
):
    from alembic import command

    from tests.conftest import get_alembic_config

    await pg_engine.dispose()
    pg_url = os.environ["STARDAG_API_TEST_DATABASE_URL"]
    alembic_cfg = get_alembic_config(pg_url)
    await asyncio.to_thread(command.downgrade, alembic_cfg, _SCOPE_DOWN_REVISION)

    # Pre-migration shape: a RUNNING build holding ``down``, a terminal build
    # holding ``old-down``, one legacy edge each, and a phantom with an edge
    # into a real task.
    async with pg_session.begin():
        up_pk = await _insert_task(pg_session, "mig-up")
        down_pk = await _insert_task(pg_session, "mig-down")
        await _insert_edge(pg_session, up_pk, down_pk)
        running_build = await _insert_build(pg_session, status="running")
        await _insert_event(pg_session, running_build, down_pk)

        old_down_pk = await _insert_task(pg_session, "mig-old-down")
        await _insert_edge(pg_session, up_pk, old_down_pk)
        done_build = await _insert_build(pg_session, status="completed")
        await _insert_event(pg_session, done_build, old_down_pk)

        phantom_pk = await _insert_task(pg_session, "mig-phantom", is_phantom=True)
        await _insert_edge(pg_session, phantom_pk, down_pk)

    await pg_engine.dispose()
    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
    pg_session.expire_all()

    # Every build carries the synthetic scope.
    for build_id in (running_build, done_build):
        scope = await _scalar(
            pg_session, "SELECT scope_key FROM builds WHERE id = :b", b=build_id
        )
        assert scope == f"build:{build_id}"

    # The running build's edge was copied into its scope; the legacy row
    # stays as history.
    copied = await _scalar(
        pg_session,
        "SELECT count(*) FROM task_dependencies WHERE upstream_task_id = :up "
        "AND downstream_task_id = :down AND scope_key = :scope",
        up=up_pk,
        down=down_pk,
        scope=f"build:{running_build}",
    )
    assert copied == 1
    legacy = await _scalar(
        pg_session,
        "SELECT count(*) FROM task_dependencies WHERE upstream_task_id = :up "
        "AND downstream_task_id = :down AND scope_key IS NULL",
        up=up_pk,
        down=down_pk,
    )
    assert legacy == 1

    # A terminal build's edges are not copied — history only.
    old_copies = await _scalar(
        pg_session,
        "SELECT count(*) FROM task_dependencies WHERE downstream_task_id = :down "
        "AND scope_key IS NOT NULL",
        down=old_down_pk,
    )
    assert old_copies == 0

    # The phantom and its edge are gone.
    assert (
        await _scalar(
            pg_session, "SELECT count(*) FROM tasks WHERE task_id = 'mig-phantom'"
        )
        == 0
    )
    assert (
        await _scalar(
            pg_session,
            "SELECT count(*) FROM task_dependencies WHERE upstream_task_id = :p",
            p=phantom_pk,
        )
        == 0
    )

    # And the running build's frontier still gates ``down`` on ``up``.
    frontier = (await pg_client.get(f"{BUILDS}/{running_build}/frontier")).json()
    assert frontier["scope_key"] == f"build:{running_build}"
    assert "mig-down" not in {t["task_id"] for t in frontier["actionable"]}, frontier
