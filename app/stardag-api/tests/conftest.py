"""Test fixtures for stardag-api."""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from alembic import command
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


async def seed_defaults(session: AsyncSession):
    """Seed default workspace, environment, user, and membership."""
    from stardag_api.models import Environment, Workspace, WorkspaceMember, User
    from stardag_api.models.enums import WorkspaceRole

    # Create default workspace
    workspace = Workspace(
        id="default",
        name="Default Workspace",
        slug="default",
    )
    session.add(workspace)

    # Create default user
    user = User(
        id="default",
        external_id="default-local-user",
        email="default@localhost",
        display_name="Default User",
    )
    session.add(user)

    # Create membership (user is owner of default workspace)
    membership = WorkspaceMember(
        id="default",
        workspace_id="default",
        user_id="default",
        role=WorkspaceRole.OWNER,
    )
    session.add(membership)

    # Create default environment
    environment = Environment(
        id="default",
        workspace_id="default",
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
        id="default",
        workspace_id="default",
        name="Default Environment",
        slug="default",
    )
    mock_user = User(
        id="default",
        external_id="default-local-user",
        email="default@localhost",
        display_name="Default User",
    )
    mock_sdk_auth = SdkAuth(
        environment=mock_environment,
        workspace_id="default",
        user=mock_user,
    )

    async def override_require_sdk_auth() -> SdkAuth:
        return mock_sdk_auth

    async def override_get_current_user() -> User:
        return mock_user

    async def override_get_current_user_flexible() -> User:
        return mock_user

    async def override_get_workspace_id_from_token() -> str:
        return "default"

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


# PostgreSQL test fixtures for integration testing with real migrations


@pytest.fixture
async def pg_engine(request):
    """Create a PostgreSQL test database engine with migrations applied.

    This fixture requires a PostgreSQL database URL to be set via the
    STARDAG_API_TEST_DATABASE_URL environment variable.

    Usage:
        STARDAG_API_TEST_DATABASE_URL=postgresql+asyncpg://... pytest -m integration
    """
    import os

    pg_url = os.environ.get("STARDAG_API_TEST_DATABASE_URL")
    if not pg_url:
        pytest.skip("PostgreSQL test database URL not configured")

    engine = create_async_engine(pg_url, echo=False)

    # Run migrations
    sync_url = pg_url.replace("+asyncpg", "")
    alembic_cfg = get_alembic_config(sync_url)
    command.upgrade(alembic_cfg, "head")

    yield engine

    # Cleanup - downgrade to base
    command.downgrade(alembic_cfg, "base")
    await engine.dispose()
