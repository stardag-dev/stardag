"""Base model classes and utilities."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from uuid_utils import uuid7


def generate_uuid7() -> UUID:
    """Generate a UUID7 (time-sortable UUID).

    Converts uuid_utils.UUID to standard uuid.UUID for compatibility.
    """
    return UUID(bytes=uuid7().bytes)


def utc_now() -> datetime:
    """Get current UTC timestamp."""
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive timestamp.

    Every timestamp we write is UTC (``utc_now``), but SQLite's DATETIME
    storage format carries no offset, so values read back from the test
    database are naive while values still in the session are aware. Mixing
    the two in a comparison raises. Postgres returns aware values
    throughout, so this is a no-op there.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


class TimestampMixin:
    """Mixin for created_at timestamp."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
