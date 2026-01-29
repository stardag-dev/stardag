"""WorkspaceMember model for user-workspace relationships."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Enum, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid7
from stardag_api.models.enums import WorkspaceRole

if TYPE_CHECKING:
    from stardag_api.models.workspace import Workspace
    from stardag_api.models.user import User


class WorkspaceMember(Base, TimestampMixin):
    """Junction table for user-workspace membership with roles.

    Each workspace must have exactly one owner.
    Users can have different roles in different workspaces.
    """

    __tablename__ = "workspace_members"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=generate_uuid7,
    )
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[WorkspaceRole] = mapped_column(
        Enum(WorkspaceRole, values_callable=lambda e: [x.value for x in e]),
        nullable=False,
        default=WorkspaceRole.MEMBER,
    )

    # Relationships
    workspace: Mapped[Workspace] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")
