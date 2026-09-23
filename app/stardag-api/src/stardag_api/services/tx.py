"""The transaction boundary of every v2 service call."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.services.errors import RecordedConflict


@asynccontextmanager
async def transaction(session: AsyncSession) -> AsyncIterator[None]:
    """Commit on success; roll back on any exception, except a
    :class:`RecordedConflict`, whose record is committed before it is
    re-raised — a refusal never erases the history of having received it."""
    try:
        yield
        await session.commit()
    except RecordedConflict:
        await session.commit()
        raise
    except BaseException:
        await session.rollback()
        raise
