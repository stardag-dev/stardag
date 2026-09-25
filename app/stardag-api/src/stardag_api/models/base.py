"""Base model classes and utilities."""

from datetime import datetime, timezone
from uuid import UUID

import enum

from sqlalchemy import DateTime, Enum, ForeignKey, Uuid, func
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


class EnvironmentScopedMixin:
    """``environment_id`` and ``created_at``, which every v2 table carries.

    ``environment_id`` is also the leading column of every composite foreign
    key between environment-scoped tables (the design's environment rule):
    a row can only point at a row of its own environment, by constraint.
    ``created_at`` has a server default as well as the Python one, so a raw
    ``INSERT … ON CONFLICT`` needs to supply neither.
    """

    environment_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )


def pg_enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    """A native Postgres enum storing the members' *values*.

    Native rather than a CHECK-constrained string: the type is the
    constraint (no CHECK to keep in step with the Python enum), Alembic's
    autogenerate sees the type, and group (b)'s enums already work this way.
    The cost, ``ALTER TYPE … ADD VALUE`` to add a member, is the right cost:
    members are only ever appended.
    """
    return Enum(
        enum_cls,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )
