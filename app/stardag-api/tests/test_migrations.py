"""The migration chain and the models describe the same schema.

The session fixture (``migrated_database_url``) builds the test database by
applying every migration to an empty ``public`` schema. Two checks that the
result is what the models say:

1. **Alembic autogenerate finds nothing to do** — no missing or extra
   table, column, index, unique key or foreign key, no type or nullability
   drift. This is what fails when a model changes without a migration.
2. **The catalog agrees too**, for what autogenerate does not compare:
   CHECK constraints, partial-index predicates, enum labels, and the
   ``ON DELETE SET NULL (col)`` column lists of the composite foreign keys,
   all of which carry v2 invariants. The models are created into a scratch schema with
   ``create_all`` and every constraint and index definition is compared,
   as Postgres itself renders them, with the migrated ``public`` schema.

A third group checks the v2 migration's data-loss guard: it is run from the
v1 head in a scratch schema (on a connection whose ``search_path`` points
there), with and without v1 rows and with and without the consent variable.
"""

from __future__ import annotations

import logging
import logging.config
import re

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from stardag_api.models import Base
from tests.conftest import get_alembic_config

SCRATCH_SCHEMA = "models_check"

_CONSTRAINTS = text(
    """
    SELECT rel.relname AS table_name, con.conname AS name,
           pg_get_constraintdef(con.oid) AS definition
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_namespace ns ON ns.oid = rel.relnamespace
    WHERE ns.nspname = :schema AND rel.relname <> 'alembic_version'
    """
)
_INDEXES = text(
    """
    SELECT tablename AS table_name, indexname AS name, indexdef AS definition
    FROM pg_indexes
    WHERE schemaname = :schema AND tablename <> 'alembic_version'
    """
)


_ENUMS = text(
    """
    SELECT 'enum' AS table_name, t.typname AS name,
           string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) AS definition
    FROM pg_type t
    JOIN pg_enum e ON e.enumtypid = t.oid
    JOIN pg_namespace ns ON ns.oid = t.typnamespace
    WHERE ns.nspname = :schema
    GROUP BY t.typname
    """
)


def _normalise(definition: str, schema: str) -> str:
    # Postgres qualifies names outside the search path; the two schemas
    # differ only in that qualifier.
    return re.sub(rf"\b{re.escape(schema)}\.", "", definition)


async def _catalog(conn, schema: str) -> dict[tuple[str, str, str], str]:
    rows: dict[tuple[str, str, str], str] = {}
    for kind, query in (
        ("constraint", _CONSTRAINTS),
        ("index", _INDEXES),
        ("type", _ENUMS),
    ):
        for table, name, definition in await conn.execute(query, {"schema": schema}):
            rows[(kind, table, name)] = _normalise(definition, schema)
    return rows


async def test_autogenerate_finds_no_difference(migrated_database_url: str):
    engine = create_async_engine(migrated_database_url)
    try:
        async with engine.connect() as conn:
            diff = await conn.run_sync(
                lambda sync_conn: compare_metadata(
                    MigrationContext.configure(sync_conn), Base.metadata
                )
            )
    finally:
        await engine.dispose()
    assert diff == [], f"models and migrations disagree: {diff}"


@pytest.fixture
async def models_schema(migrated_database_url: str):
    """The models, created with ``create_all`` into a scratch schema."""
    engine = create_async_engine(migrated_database_url)
    async with engine.begin() as conn:
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCRATCH_SCHEMA} CASCADE"))
        await conn.execute(text(f"CREATE SCHEMA {SCRATCH_SCHEMA}"))
        translated = await conn.execution_options(
            schema_translate_map={None: SCRATCH_SCHEMA}
        )
        await translated.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCRATCH_SCHEMA} CASCADE"))
        await engine.dispose()


async def test_catalog_matches_models(models_schema):
    async with models_schema.connect() as conn:
        migrated = await _catalog(conn, "public")
        modelled = await _catalog(conn, SCRATCH_SCHEMA)

    only_migrated = sorted(set(migrated) - set(modelled))
    only_modelled = sorted(set(modelled) - set(migrated))
    differing = {
        key: (migrated[key], modelled[key])
        for key in sorted(set(migrated) & set(modelled))
        if migrated[key] != modelled[key]
    }
    assert not only_migrated, f"only in the migrated schema: {only_migrated}"
    assert not only_modelled, f"only in the models: {only_modelled}"
    assert not differing, f"definitions differ (migrated, modelled): {differing}"


# --- The v2 migration's data-loss guard ------------------------------------

V1_HEAD = "a3c1f0d47b28"  # the v2 core-schema revision's down_revision
V2_CORE = "630d475de408"
GUARD_SCHEMA = "v1_guard_check"
ACCEPT_ENV = "STARDAG_ACCEPT_V2_DATA_LOSS"


async def _in_guard_schema(conn) -> None:
    await conn.execute(text(f"SET LOCAL search_path TO {GUARD_SCHEMA}"))


async def _migrate(engine, revision: str) -> None:
    """``alembic upgrade <revision>`` in the scratch schema, in one transaction."""
    config = get_alembic_config(engine.url.render_as_string(hide_password=False))

    def upgrade(sync_conn) -> None:
        config.attributes["connection"] = sync_conn
        command.upgrade(config, revision)

    async with engine.begin() as conn:
        await _in_guard_schema(conn)
        await conn.run_sync(upgrade)


async def _scalar(engine, sql: str):
    async with engine.begin() as conn:
        await _in_guard_schema(conn)
        return await conn.scalar(text(sql))


@pytest.fixture
async def v1_schema(migrated_database_url: str):
    """An empty scratch schema migrated to the last v1 revision."""
    engine = create_async_engine(migrated_database_url)
    async with engine.begin() as conn:
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {GUARD_SCHEMA} CASCADE"))
        await conn.execute(text(f"CREATE SCHEMA {GUARD_SCHEMA}"))
    await _migrate(engine, V1_HEAD)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {GUARD_SCHEMA} CASCADE"))
        await engine.dispose()


async def _seed_v1_rows(engine) -> None:
    """One v1 build and one v1 task, with the rows they need."""
    async with engine.begin() as conn:
        await _in_guard_schema(conn)
        for statement in (
            "INSERT INTO workspaces (id, name, slug, created_at)"
            " VALUES ('00000000-0000-0000-0000-00000000000a', 'w', 'w', now())",
            "INSERT INTO environments (id, workspace_id, name, slug, created_at)"
            " VALUES ('00000000-0000-0000-0000-00000000000b',"
            " '00000000-0000-0000-0000-00000000000a', 'e', 'e', now())",
            "INSERT INTO builds (id, environment_id, name, root_task_ids,"
            " last_active_at, created_at, scope_key, latest_is_resumed,"
            " latest_status) VALUES (gen_random_uuid(),"
            " '00000000-0000-0000-0000-00000000000b', 'b', '[]', now(), now(),"
            " 's', false, 'pending')",
            "INSERT INTO tasks (id, environment_id, task_id, task_namespace,"
            " task_name, task_data, created_at, latest_status,"
            " latest_waiting_for_lock) VALUES (gen_random_uuid(),"
            " '00000000-0000-0000-0000-00000000000b', 't', '', 'T', '{}', now(),"
            " 'pending', false)",
        ):
            await conn.execute(text(statement))


@pytest.mark.parametrize("consent", [None, "true"])
async def test_v2_migration_refuses_v1_rows_without_consent(
    v1_schema, monkeypatch, consent
):
    """Only exactly "1" consents; the refusal drops nothing."""
    await _seed_v1_rows(v1_schema)
    if consent is None:
        monkeypatch.delenv(ACCEPT_ENV, raising=False)
    else:
        monkeypatch.setenv(ACCEPT_ENV, consent)

    with pytest.raises(RuntimeError) as refused:
        await _migrate(v1_schema, "head")

    message = str(refused.value)
    assert "(1 builds, 1 tasks in this database)" in message
    assert "pg_dump" in message
    assert f"{ACCEPT_ENV}=1" in message
    assert "RELEASE_NOTES.md" in message
    # Nothing dropped: the v1 tables still hold their rows, the revision
    # did not advance, and no v2 table was created.
    assert await _scalar(v1_schema, "SELECT count(*) FROM builds") == 1
    assert await _scalar(v1_schema, "SELECT count(*) FROM tasks") == 1
    assert await _scalar(v1_schema, "SELECT version_num FROM alembic_version") == (
        V1_HEAD
    )
    assert await _scalar(v1_schema, "SELECT to_regclass('build')") is None


async def test_v2_migration_drops_v1_rows_with_consent(v1_schema, monkeypatch, caplog):
    await _seed_v1_rows(v1_schema)
    monkeypatch.setenv(ACCEPT_ENV, "1")

    # env.py's fileConfig would replace every alembic logger's handlers,
    # caplog's with them; the log line is what is under test, not that.
    monkeypatch.setattr(logging.config, "fileConfig", lambda *a, **k: None)
    with caplog.at_level(logging.INFO, logger="alembic"):
        await _migrate(v1_schema, V2_CORE)

    # The roll-out log records what the accepted loss dropped.
    (accepted,) = [r for r in caplog.records if ACCEPT_ENV in r.getMessage()]
    assert accepted.levelno == logging.INFO
    assert "Dropping 1 v1 builds and 1 v1 tasks" in accepted.getMessage()

    assert await _scalar(v1_schema, "SELECT to_regclass('builds')") is None
    assert await _scalar(v1_schema, "SELECT to_regclass('tasks')") is None
    assert await _scalar(v1_schema, "SELECT count(*) FROM build") == 0
    assert await _scalar(v1_schema, "SELECT count(*) FROM environments") == 1


async def test_v2_migration_needs_no_consent_on_an_empty_v1_schema(
    v1_schema, monkeypatch
):
    monkeypatch.delenv(ACCEPT_ENV, raising=False)

    await _migrate(v1_schema, "head")

    assert await _scalar(v1_schema, "SELECT to_regclass('builds')") is None
    assert await _scalar(v1_schema, "SELECT to_regclass('build')") is not None
