"""Test fixtures for stardag-api.

**The API suite runs against Postgres, always** (engineering rule 5 of the
v2 line: SQLite-only behaviour is not evidence). There is no SQLite
fallback: the v2 schema uses native enums, JSONB and composite foreign keys
with column-list ``ON DELETE SET NULL``, none of which SQLite has, and a
test that passed on a different database would say nothing about this one.

Where the database comes from:

- ``STARDAG_API_TEST_DATABASE_URL`` if set (CI sets it), else
- :data:`DEFAULT_TEST_DATABASE_URL`, the repository's ``docker compose``
  Postgres (``docker compose up -d db`` from the repository root), in a
  database of its own that is created on first use.

Once per session the database's ``public`` schema is dropped and the whole
Alembic chain is applied to it (``upgrade head``), so every run exercises
the migrations, not ``metadata.create_all``. Each test then starts from
empty tables plus the seeded defaults (``TRUNCATE`` is cheap; re-migrating
per test is not). A test that needs no database requests none of these
fixtures and runs without Postgres; one that does fails the session with a
message saying how to provide it, rather than skipping.
"""

import asyncio
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from stardag_api.db import get_db
from stardag_api.main import app
from stardag_api.models import Base

# The docker-compose Postgres (superuser ``stardag``), in a database of its
# own so the suite never touches the development ``stardag`` database.
DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://stardag:stardag@localhost:5432/stardag_api_test"
)


def test_database_url() -> str:
    """The Postgres URL the suite runs against."""
    return os.environ.get("STARDAG_API_TEST_DATABASE_URL") or DEFAULT_TEST_DATABASE_URL


# Not a test, despite the name pytest would otherwise collect it by.
test_database_url.__test__ = False  # type: ignore[attr-defined]


def get_alembic_config(connection_url: str | None = None) -> Config:
    """Get alembic config for running migrations."""
    base_path = Path(__file__).parent.parent
    alembic_cfg = Config(str(base_path / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(base_path / "migrations"))
    if connection_url:
        alembic_cfg.set_main_option("sqlalchemy.url", connection_url)
    return alembic_cfg


# Fixed UUIDs for test fixtures (deterministic for reproducibility)
DEFAULT_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
DEFAULT_WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000002")
DEFAULT_ENVIRONMENT_ID = UUID("00000000-0000-0000-0000-000000000003")
DEFAULT_MEMBERSHIP_ID = UUID("00000000-0000-0000-0000-000000000004")

# String versions for test assertions (JSON responses serialize UUIDs to strings)
DEFAULT_USER_ID_STR = str(DEFAULT_USER_ID)
DEFAULT_WORKSPACE_ID_STR = str(DEFAULT_WORKSPACE_ID)
DEFAULT_ENVIRONMENT_ID_STR = str(DEFAULT_ENVIRONMENT_ID)


async def seed_defaults(session: AsyncSession):
    """Seed default workspace, environment, user, and membership."""
    from stardag_api.models import Environment, Workspace, WorkspaceMember, User
    from stardag_api.models.enums import WorkspaceRole

    # Create default workspace
    workspace = Workspace(
        id=DEFAULT_WORKSPACE_ID,
        name="Default Workspace",
        slug="default",
    )
    session.add(workspace)

    # Create default user
    user = User(
        id=DEFAULT_USER_ID,
        external_id="default-local-user",
        email="default@localhost",
        display_name="Default User",
    )
    session.add(user)

    # Create membership (user is owner of default workspace)
    membership = WorkspaceMember(
        id=DEFAULT_MEMBERSHIP_ID,
        workspace_id=DEFAULT_WORKSPACE_ID,
        user_id=DEFAULT_USER_ID,
        role=WorkspaceRole.OWNER,
    )
    session.add(membership)

    # Create default environment
    environment = Environment(
        id=DEFAULT_ENVIRONMENT_ID,
        workspace_id=DEFAULT_WORKSPACE_ID,
        name="Default Environment",
        slug="default",
    )
    session.add(environment)

    await session.commit()


async def _ensure_database(url: str) -> None:
    """Create the test database if it does not exist yet.

    Needs a role allowed to create databases (the compose superuser is);
    CI creates its database up front, so this is a no-op there.
    """
    target = make_url(url)
    maintenance = create_async_engine(
        target.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        async with maintenance.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        await maintenance.dispose()


async def _reset_schema(url: str) -> None:
    """Drop and recreate ``public``, so the migration chain starts empty.

    A downgrade-to-base would not do: the v2 migration has no downgrade,
    and the initial one does not drop its enum types.
    """
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def migrated_database_url() -> str:
    """A Postgres database migrated to head from empty, once per session.

    Synchronous on purpose: Alembic's ``env.py`` drives its async runner
    with ``asyncio.run``, which cannot be nested in a running event loop.
    """
    url = test_database_url()
    try:
        asyncio.run(_ensure_database(url))
        asyncio.run(_reset_schema(url))
    except (OSError, ConnectionError) as exc:  # refused, unresolvable, ...
        pytest.exit(
            "The stardag-api suite needs Postgres and none is reachable at "
            f"{make_url(url).render_as_string(hide_password=True)} ({exc}).\n"
            "Start the compose one with `docker compose up -d db` from the "
            "repository root, or point STARDAG_API_TEST_DATABASE_URL at a "
            "Postgres you can create a database in.",
            returncode=2,
        )
    command.upgrade(get_alembic_config(url), "head")
    return url


async def _truncate_all(engine) -> None:
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))


@pytest.fixture
async def async_engine(migrated_database_url: str):
    """An engine on the migrated test database: empty tables plus defaults."""
    engine = create_async_engine(migrated_database_url, echo=False)
    await _truncate_all(engine)
    async_session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session_maker() as session:
        await seed_defaults(session)

    yield engine
    await engine.dispose()


@pytest.fixture
async def async_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create a test database session."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with async_session_maker() as session:
        yield session


@pytest.fixture
async def client(async_engine) -> AsyncGenerator[AsyncClient, None]:
    """Create a test client with isolated database and mocked auth."""
    from stardag_api.auth import (
        SdkAuth,
        get_current_user,
        get_current_user_flexible,
        get_workspace_id_from_token,
        require_sdk_auth,
    )
    from stardag_api.models import Environment, User

    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with async_session_maker() as session:
            yield session

    # Create mock auth objects
    mock_environment = Environment(
        id=DEFAULT_ENVIRONMENT_ID,
        workspace_id=DEFAULT_WORKSPACE_ID,
        name="Default Environment",
        slug="default",
    )
    mock_user = User(
        id=DEFAULT_USER_ID,
        external_id="default-local-user",
        email="default@localhost",
        display_name="Default User",
    )
    mock_sdk_auth = SdkAuth(
        environment=mock_environment,
        workspace_id=DEFAULT_WORKSPACE_ID,
        user=mock_user,
    )

    async def override_require_sdk_auth() -> SdkAuth:
        return mock_sdk_auth

    async def override_get_current_user() -> User:
        return mock_user

    async def override_get_current_user_flexible() -> User:
        return mock_user

    async def override_get_workspace_id_from_token() -> UUID:
        return DEFAULT_WORKSPACE_ID

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_sdk_auth] = override_require_sdk_auth
    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_current_user_flexible] = (
        override_get_current_user_flexible
    )
    app.dependency_overrides[get_workspace_id_from_token] = (
        override_get_workspace_id_from_token
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
async def unauthenticated_client(async_engine) -> AsyncGenerator[AsyncClient, None]:
    """Create a test client without mocked authentication.

    Use this for tests that verify authentication is required.
    """
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with async_session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clear_limits_caches():
    """Clear in-memory caches between tests to prevent cross-test pollution."""
    from stardag_api.limits import _entity_cache, _rate_limiter
    from stardag_api.services.api_keys import _validation_cache

    _rate_limiter.clear()
    _entity_cache.clear()
    _validation_cache.clear()
    yield
    _rate_limiter.clear()
    _entity_cache.clear()
    _validation_cache.clear()


# Alternate-identity fixtures. Every write endpoint has two boundaries worth
# testing: the environment (the tenancy boundary for builds/tasks/limits) and
# the workspace role (the admin gate on destructive operations). These live
# here rather than in one test module because several do.


@pytest.fixture
async def as_environment_b(async_engine):
    """Context manager switching the app's auth override to a SECOND
    environment in the same workspace (the tenancy boundary for
    tasks/limits is the environment)."""
    import contextlib
    from uuid import UUID

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from stardag_api.auth import SdkAuth, require_sdk_auth
    from stardag_api.main import app
    from stardag_api.models import Environment, User
    from tests.conftest import DEFAULT_USER_ID, DEFAULT_WORKSPACE_ID

    env_b_id = UUID("00000000-0000-0000-0000-00000000000b")
    session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with session_maker() as session:
        session.add(
            Environment(
                id=env_b_id,
                workspace_id=DEFAULT_WORKSPACE_ID,
                name="Environment B",
                slug="env-b",
            )
        )
        await session.commit()

    auth_b = SdkAuth(
        environment=Environment(
            id=env_b_id, workspace_id=DEFAULT_WORKSPACE_ID, name="Environment B"
        ),
        workspace_id=DEFAULT_WORKSPACE_ID,
        user=User(
            id=DEFAULT_USER_ID,
            external_id="default-local-user",
            email="default@localhost",
            display_name="Default User",
        ),
    )

    async def override_require_sdk_auth_b() -> SdkAuth:
        return auth_b

    @contextlib.contextmanager
    def _switch():
        previous = app.dependency_overrides[require_sdk_auth]
        app.dependency_overrides[require_sdk_auth] = override_require_sdk_auth_b
        try:
            yield
        finally:
            app.dependency_overrides[require_sdk_auth] = previous

    return _switch


@pytest.fixture
async def role_auth_switcher(async_engine):
    """Context-manager factory switching the app's auth override between a
    MEMBER-role user and an API-key (machine) credential in the default
    environment. The default ``client`` auth (OWNER-role user) is restored
    on exit."""
    import contextlib
    from uuid import UUID

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from stardag_api.auth import SdkAuth, require_sdk_auth
    from stardag_api.main import app
    from stardag_api.models import Environment, User, WorkspaceMember
    from stardag_api.models.enums import WorkspaceRole
    from tests.conftest import DEFAULT_ENVIRONMENT_ID, DEFAULT_WORKSPACE_ID

    member_user_id = UUID("00000000-0000-0000-0000-0000000000ae")
    session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with session_maker() as session:
        session.add(
            User(
                id=member_user_id,
                external_id="member-user",
                email="member@localhost",
                display_name="Member User",
            )
        )
        session.add(
            WorkspaceMember(
                workspace_id=DEFAULT_WORKSPACE_ID,
                user_id=member_user_id,
                role=WorkspaceRole.MEMBER,
            )
        )
        await session.commit()

    environment = Environment(
        id=DEFAULT_ENVIRONMENT_ID,
        workspace_id=DEFAULT_WORKSPACE_ID,
        name="Default Environment",
    )
    member_auth = SdkAuth(
        environment=environment,
        workspace_id=DEFAULT_WORKSPACE_ID,
        user=User(
            id=member_user_id,
            external_id="member-user",
            email="member@localhost",
            display_name="Member User",
        ),
    )
    # API-key auth context: no user attached (machine credential).
    api_key_auth = SdkAuth(
        environment=environment,
        workspace_id=DEFAULT_WORKSPACE_ID,
        user=None,
    )

    @contextlib.contextmanager
    def _as(auth: SdkAuth):
        async def _override() -> SdkAuth:
            return auth

        previous = app.dependency_overrides[require_sdk_auth]
        app.dependency_overrides[require_sdk_auth] = _override
        try:
            yield
        finally:
            app.dependency_overrides[require_sdk_auth] = previous

    return {"member": lambda: _as(member_auth), "api_key": lambda: _as(api_key_auth)}


@pytest.fixture
def session_factory(async_engine) -> async_sessionmaker[AsyncSession]:
    """A session factory on the migrated test database.

    The v2 services commit their own transaction, so a test opens a fresh
    session per call — and two of them when it needs two concurrent
    transactions.
    """
    return async_sessionmaker(async_engine, expire_on_commit=False)
