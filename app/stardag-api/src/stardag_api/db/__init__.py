from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from stardag_api.config import settings

# Pool sizing: pool_size + max_overflow caps simultaneous connections per
# process. With multiple uvicorn workers per ECS task this multiplies; sized
# to stay well under Aurora's max_connections at the lowest ACU we run.
# pool_pre_ping handles Aurora idle-timeouts and quiet failovers transparently.
# pool_recycle forces a fresh connection at least every 30 min, defending
# against silent NLB / Aurora-side disconnects.
_engine_kwargs: dict[str, object] = {
    "pool_size": 10,
    "max_overflow": 20,
    "pool_pre_ping": True,
    "pool_recycle": 1800,
}

# SQLite (used by the in-memory test DB) doesn't support these pool settings;
# pass them only for real DB backends.
if settings.effective_database_url.startswith("sqlite"):
    _engine_kwargs = {}

engine = create_async_engine(settings.effective_database_url, **_engine_kwargs)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session
