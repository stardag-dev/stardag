"""The v2 schema's foreign-key actions and the environment rule, on Postgres.

Not the registration or transition invariants (those are pinned with the
services that enforce them): only what the schema decides on its own, by
constraint, and which is easy to get subtly wrong in DDL.

- A row cannot point into another environment (every FK between
  environment-scoped tables carries ``environment_id``).
- Deleting a build cascades its plans, members and executions, while its
  events survive with ``build_id``/``plan_id``/``execution_id`` NULL and a
  task that named one of its plans as claim holder keeps its row — the
  column-list ``ON DELETE SET NULL (col)`` form, which nulls only the
  pointer and not the composite key's other columns (S17).
- ``deployment`` is ``ON DELETE RESTRICT`` from everywhere, yet deleting a
  whole environment still works.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import DEFAULT_ENVIRONMENT_ID, DEFAULT_WORKSPACE_ID

OTHER_ENVIRONMENT_ID = UUID("00000000-0000-0000-0000-0000000000e2")
SETTINGS_HASH = "0" * 64


async def _exec(session: AsyncSession, sql: str, **params) -> None:
    await session.execute(text(sql), params)


async def _scope(session: AsyncSession, env: UUID) -> UUID:
    """A deployment and the empty settings in ``env``; returns the deployment."""
    deployment_id = uuid4()
    await _exec(
        session,
        "INSERT INTO deployment (id, environment_id, kind, app_name, code_id,"
        " deployed_at, generation, activated_at)"
        " VALUES (:id, :env, 'local', 'app', :code, now(), 1, now())",
        id=deployment_id,
        env=env,
        code=str(deployment_id),
    )
    await _exec(
        session,
        "INSERT INTO settings (environment_id, hash, body)"
        " VALUES (:env, :hash, '{}') ON CONFLICT DO NOTHING",
        env=env,
        hash=SETTINGS_HASH,
    )
    return deployment_id


async def _claimed_member(session: AsyncSession, env: UUID) -> dict[str, UUID]:
    """build -> plan -> member, with an execution holding the task's claim."""
    ids = {k: uuid4() for k in ("build", "plan", "task", "instance", "execution")}
    ids["deployment"] = await _scope(session, env)
    await _exec(
        session,
        "INSERT INTO build (id, environment_id, name, root_task_ids, last_active_at)"
        " VALUES (:build, :env, 'b', '[]', now())",
        env=env,
        **ids,
    )
    await _exec(
        session,
        "INSERT INTO plan (id, environment_id, build_id, deployment_id,"
        " settings_hash, generation, activated_at)"
        " VALUES (:plan, :env, :build, :deployment, :hash, 1, now())",
        env=env,
        hash=SETTINGS_HASH,
        **ids,
    )
    await _exec(
        session,
        "INSERT INTO task (id, environment_id, task_id, task_name)"
        " VALUES (:task, :env, :task_id, 'T')",
        env=env,
        task_id=str(ids["task"]),
        **ids,
    )
    await _exec(
        session,
        "INSERT INTO task_instance (id, environment_id, deployment_id,"
        " settings_hash, instance_hash, task_pk, body, expanded_at)"
        " VALUES (:instance, :env, :deployment, :hash, 'h', :task, '{}', now())",
        env=env,
        hash=SETTINGS_HASH,
        **ids,
    )
    await _exec(
        session,
        "INSERT INTO plan_member (environment_id, plan_id, task_pk, instance_id,"
        " deployment_id, settings_hash, is_root, admitted_by)"
        " VALUES (:env, :plan, :task, :instance, :deployment, :hash, true, 'root')",
        env=env,
        hash=SETTINGS_HASH,
        **ids,
    )
    await _exec(
        session,
        "INSERT INTO execution (id, environment_id, task_pk, plan_id,"
        " instance_id, started_at)"
        " VALUES (:execution, :env, :task, :plan, :instance, now())",
        env=env,
        **ids,
    )
    await _exec(
        session,
        "UPDATE task SET status = 'running', claim_expires_at = :expires,"
        " claim_plan_id = :plan, execution_id = :execution WHERE id = :task",
        expires=datetime.now(timezone.utc) + timedelta(hours=1),
        **ids,
    )
    for event_type, plan in (("build_started", None), ("task_started", ids["plan"])):
        await _exec(
            session,
            "INSERT INTO event (id, environment_id, build_id, task_pk, plan_id,"
            " execution_id, event_type) VALUES (:id, :env, :build, :task_pk,"
            " :plan_id, :execution_id, :type)",
            id=uuid4(),
            env=env,
            build=ids["build"],
            task_pk=ids["task"] if plan else None,
            plan_id=plan,
            execution_id=ids["execution"] if plan else None,
            type=event_type,
        )
    return ids


@pytest.fixture
async def other_environment(async_session: AsyncSession) -> UUID:
    await _exec(
        async_session,
        "INSERT INTO environments (id, workspace_id, name, slug, created_at)"
        " VALUES (:id, :ws, 'Other', 'other', now())",
        id=OTHER_ENVIRONMENT_ID,
        ws=DEFAULT_WORKSPACE_ID,
    )
    await async_session.commit()
    return OTHER_ENVIRONMENT_ID


async def test_a_row_cannot_point_into_another_environment(
    async_session: AsyncSession, other_environment: UUID
):
    ids = await _claimed_member(async_session, DEFAULT_ENVIRONMENT_ID)
    await async_session.commit()
    # An instance in the other environment naming this environment's task
    # and deployment: refused by the composite FKs, not by a query.
    await _scope(async_session, other_environment)
    with pytest.raises(IntegrityError, match="fk_task_instance_"):
        await _exec(
            async_session,
            "INSERT INTO task_instance (id, environment_id, deployment_id,"
            " settings_hash, instance_hash, task_pk, body)"
            " VALUES (:id, :env, :deployment, :hash, 'x', :task, '{}')",
            id=uuid4(),
            env=other_environment,
            hash=SETTINGS_HASH,
            **{k: ids[k] for k in ("deployment", "task")},
        )


async def test_deleting_a_build_cascades_its_plans_and_keeps_history(
    async_session: AsyncSession,
):
    ids = await _claimed_member(async_session, DEFAULT_ENVIRONMENT_ID)
    await async_session.commit()

    await _exec(async_session, "DELETE FROM build WHERE id = :build", **ids)
    await async_session.commit()

    for table in ("plan", "plan_member", "execution"):
        count = await async_session.scalar(text(f"SELECT count(*) FROM {table}"))
        assert count == 0, table
    task = (
        await async_session.execute(
            text(
                "SELECT environment_id, claim_plan_id, execution_id"
                " FROM task WHERE id = :task"
            ),
            ids,
        )
    ).one()
    assert task == (DEFAULT_ENVIRONMENT_ID, None, None)
    events = (
        await async_session.execute(
            text(
                "SELECT environment_id, build_id, plan_id, execution_id, task_pk"
                " FROM event ORDER BY created_at, event_type"
            )
        )
    ).all()
    assert len(events) == 2
    assert {e[:4] for e in events} == {(DEFAULT_ENVIRONMENT_ID, None, None, None)}


async def test_deployment_is_restricted_but_an_environment_can_be_deleted(
    async_session: AsyncSession, other_environment: UUID
):
    ids = await _claimed_member(async_session, other_environment)
    await async_session.commit()

    with pytest.raises(IntegrityError, match="fk_(task_instance|plan)_deployment"):
        await _exec(
            async_session, "DELETE FROM deployment WHERE id = :deployment", **ids
        )
    await async_session.rollback()

    await _exec(
        async_session, "DELETE FROM environments WHERE id = :id", id=other_environment
    )
    await async_session.commit()
    remaining = await async_session.scalar(
        text("SELECT count(*) FROM deployment WHERE environment_id = :env"),
        {"env": other_environment},
    )
    assert remaining == 0
