"""Workspace model for multi-tenancy."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid7

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

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=generate_uuid7,
    )
    name: Mapped[str] = mapped_column(
        String(255),
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
    created_by_id: Mapped[UUID | None] = mapped_column(
        Uuid,
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

    # The hosting operator's override of the v2 write rate limit for this
    # workspace (requests per minute per API instance; NULL: the
    # deployment's ``LIMITS_MAX_REQUESTS_PER_MINUTE``). A cost control of
    # whoever runs the registry, set directly in the database: deliberately
    # absent from every API schema, so no workspace or org admin can read or
    # change it (STA-135).
    max_write_requests_per_minute: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
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
