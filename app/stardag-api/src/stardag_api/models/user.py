"""User model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid7

if TYPE_CHECKING:
    from stardag_api.models.build import Build
    from stardag_api.models.workspace_member import WorkspaceMember


class User(Base, TimestampMixin):
    """User entity.

    Users are created automatically on first OIDC login.
    A user can belong to multiple workspaces via WorkspaceMember.
    """

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=generate_uuid7,
    )
    # OIDC subject claim - unique identifier from identity provider
    external_id: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
    )
    display_name: Mapped[str | None] = mapped_column(String(255))
    # bcrypt hash, only set for local-auth users (null for OIDC users)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    # Set on password change; session tokens issued before this instant are
    # rejected, so a stolen session token doesn't survive a password change.
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # Relationships
    memberships: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    builds: Mapped[list[Build]] = relationship(back_populates="user")
