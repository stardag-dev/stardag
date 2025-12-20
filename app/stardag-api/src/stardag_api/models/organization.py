"""Organization model for multi-tenancy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from stardag_api.models.invite import Invite
    from stardag_api.models.organization_member import OrganizationMember
    from stardag_api.models.workspace import Workspace


class Organization(Base, TimestampMixin):
    """Multi-tenancy root entity.

    Users belong to organizations via OrganizationMember with roles.
    """

    __tablename__ = "organizations"

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

    # Relationships
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
