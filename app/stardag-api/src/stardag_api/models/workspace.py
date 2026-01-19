"""Workspace model for multi-tenancy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from stardag_api.models.invite import Invite
    from stardag_api.models.workspace_member import WorkspaceMember
    from stardag_api.models.user import User
    from stardag_api.models.environment import Environment


class Workspace(Base, TimestampMixin):
    """Multi-tenancy root entity.

    Users belong to workspaces via WorkspaceMember with roles.
    """

    __tablename__ = "workspaces"

    # Limits
    MAX_WORKSPACES_PER_USER = 3
    MAX_ENVIRONMENTS_PER_WORKSPACE = 6

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
    is_personal: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )

    # Relationships
    created_by: Mapped[User | None] = relationship()
    environments: Mapped[list[Environment]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
    members: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
    invites: Mapped[list[Invite]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
