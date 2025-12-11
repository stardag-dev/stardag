"""User model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from stardag_api.models.build import Build
    from stardag_api.models.organization import Organization


class User(Base, TimestampMixin):
    """User entity.

    Defaults to 'default' user. Prepared for future authentication integration.
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("organization_id", "username", name="uq_user_org_username"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=generate_uuid,
    )
    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))

    # Relationships
    organization: Mapped[Organization] = relationship(back_populates="users")
    builds: Mapped[list[Build]] = relationship(back_populates="user")
