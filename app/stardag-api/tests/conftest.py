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
    """Create a test client with isolated database."""
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
