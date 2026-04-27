"""Test fixtures for stardag-api."""

from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import UUID

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from stardag_api.db import get_db
from stardag_api.main import app
from stardag_api.models import Base

# Use in-memory SQLite for tests
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


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


@pytest.fixture
async def async_engine():
    """Create a test database engine with schema initialized.

    For SQLite tests, we use Base.metadata.create_all() since the SQL migrations
    are PostgreSQL-specific. In CI/integration tests against PostgreSQL, alembic
    migrations should be used instead.
    """
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed defaults
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

    _rate_limiter.clear()
    _entity_cache.clear()
    yield
    _rate_limiter.clear()
    _entity_cache.clear()


# PostgreSQL test fixtures for integration testing with real migrations


@pytest.fixture
async def pg_engine():
    """Create a PostgreSQL test database engine with the latest schema.

    Requires STARDAG_API_TEST_DATABASE_URL to be set, e.g.::

        STARDAG_API_TEST_DATABASE_URL=postgresql+asyncpg://... pytest

    The schema is built with ``Base.metadata.create_all``; alembic migrations
    are not applied here because ``migrations/env.py`` calls ``asyncio.run``,
    which deadlocks when called from inside this async fixture's event loop.
    The model classes use ``JSON().with_variant(JSONB, "postgresql")`` so
    create_all on Postgres builds JSONB columns, matching what migrations
    produce in production. Migrations themselves are exercised end-to-end via
    the docker-compose pipeline, not in pytest.
    """
    import os

    pg_url = os.environ.get("STARDAG_API_TEST_DATABASE_URL")
    if not pg_url:
        pytest.skip("PostgreSQL test database URL not configured")

    engine = create_async_engine(pg_url, echo=False)
    async with engine.begin() as conn:
        # Drop everything first to reset state across runs.
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    # Seed defaults so tests share the SQLite fixture's mental model.
    async_session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session_maker() as session:
        await seed_defaults(session)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def pg_session(pg_engine) -> AsyncGenerator[AsyncSession, None]:
    """Postgres session paired with the pg_engine fixture."""
    async_session_maker = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with async_session_maker() as session:
        yield session
