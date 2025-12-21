"""Organization model for multi-tenancy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from stardag_api.models.invite import Invite
    from stardag_api.models.organization_member import OrganizationMember
    from stardag_api.models.user import User
    from stardag_api.models.workspace import Workspace


class Organization(Base, TimestampMixin):
    """Multi-tenancy root entity.

    Users belong to organizations via OrganizationMember with roles.
    """

    __tablename__ = "organizations"

    # Limits
    MAX_ORGS_PER_USER = 3
    MAX_WORKSPACES_PER_ORG = 6

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=generate_uuid,
    )
    name: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
    )
    slug: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
    )
    description: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Relationships
    created_by: Mapped[User | None] = relationship()
    workspaces: Mapped[list[Workspace]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
    )
    members: Mapped[list[OrganizationMember]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
    )
    invites: Mapped[list[Invite]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
    )
