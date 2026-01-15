"""ApiKey model for SDK authentication."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from stardag_api.models.user import User
    from stardag_api.models.environment import Environment


class ApiKey(Base, TimestampMixin):
    """API key for SDK authentication.

    Keys are scoped to an environment. The actual key is hashed;
    only the prefix (first 8 chars) is stored for identification.
    The full key is shown once on creation and cannot be retrieved.
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=generate_uuid,
    )
    environment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    # First 8 characters of the key for display/identification
    key_prefix: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        index=True,
    )
    # bcrypt hash of the full key
    key_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    created_by_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        index=True,
    )

    # Relationships
    environment: Mapped[Environment] = relationship(back_populates="api_keys")
    created_by: Mapped[User | None] = relationship()

    @property
    def is_active(self) -> bool:
        """Check if the API key is active (not revoked)."""
        return self.revoked_at is None
