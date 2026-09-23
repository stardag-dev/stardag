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
"""

from __future__ import annotations

import re

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from stardag_api.models import Base

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
