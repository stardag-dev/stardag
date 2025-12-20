"""Invite model for organization invitations."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid
from stardag_api.models.enums import InviteStatus, OrganizationRole

if TYPE_CHECKING:
    from stardag_api.models.organization import Organization
    from stardag_api.models.user import User


class Invite(Base, TimestampMixin):
    """Invitation to join an organization.

    Invites are sent by email. The invited user doesn't need to exist yet.
    When a user signs up with the invited email, they can accept pending invites.
    """

    __tablename__ = "invites"
    __table_args__ = (
        # Partial unique index: only one pending invite per org+email
        Index(
            "ix_invite_org_email_pending",
            "organization_id",
            "email",
            unique=True,
            postgresql_where="status = 'pending'",
        ),
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
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    role: Mapped[OrganizationRole] = mapped_column(
        Enum(OrganizationRole),
        nullable=False,
        default=OrganizationRole.MEMBER,
    )
    status: Mapped[InviteStatus] = mapped_column(
        Enum(InviteStatus),
        nullable=False,
        default=InviteStatus.PENDING,
        index=True,
    )
    invited_by_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    organization: Mapped[Organization] = relationship(back_populates="invites")
    invited_by: Mapped[User | None] = relationship()
