import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from stardag_api.config import settings
from stardag_api.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Set the database URL from settings. Uses the migration URL, which prefers
# STARDAG_API_DATABASE_URL_DIRECT when set: migrations must bypass
# transaction-mode connection poolers (e.g. Neon's pooled endpoint). If a
# caller has already supplied sqlalchemy.url via the Config object (e.g. a
# pytest fixture pointing at a temporary test DB), respect that instead —
# otherwise the test would silently migrate the developer's local dev DB.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.effective_migration_database_url)

# Interpret the config file for Python logging.
#
# disable_existing_loggers=False is load-bearing, not a style choice.
# fileConfig defaults it to True, which sets .disabled on every logger that
# already exists and is not named in alembic.ini — i.e. every stardag_api.*
# logger. Alembic is routinely run *in-process* (the test suite, and any
# migrate-then-serve startup), so the default silently kills application
# logging for the rest of that process, long after the migration finished.
# It surfaced as a test asserting on a log line that was emitted but never
# recorded, only when a Postgres test had run first.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# add your model's MetaData object here for 'autogenerate' support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
