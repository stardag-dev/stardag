"""Invite model for workspace invitations."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid7
from stardag_api.models.enums import InviteStatus, WorkspaceRole

if TYPE_CHECKING:
    from stardag_api.models.workspace import Workspace
    from stardag_api.models.user import User


class Invite(Base, TimestampMixin):
    """Invitation to join a workspace.

    Invites are sent by email. The invited user doesn't need to exist yet.
    When a user signs up with the invited email, they can accept pending invites.
    """

    __tablename__ = "invites"
    __table_args__ = (
        # Partial unique index: only one pending invite per workspace+email
        Index(
            "ix_invite_workspace_email_pending",
            "workspace_id",
            "email",
            unique=True,
            postgresql_where="status = 'pending'",
        ),
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
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    role: Mapped[WorkspaceRole] = mapped_column(
        Enum(WorkspaceRole, values_callable=lambda e: [x.value for x in e]),
        nullable=False,
        default=WorkspaceRole.MEMBER,
    )
    status: Mapped[InviteStatus] = mapped_column(
        Enum(InviteStatus, values_callable=lambda e: [x.value for x in e]),
        nullable=False,
        default=InviteStatus.PENDING,
        index=True,
    )
    invited_by_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    workspace: Mapped[Workspace] = relationship(back_populates="invites")
    invited_by: Mapped[User | None] = relationship()
