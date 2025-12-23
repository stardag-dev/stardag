"""TargetRoot model for storage location configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from stardag_api.models.workspace import Workspace


class TargetRoot(Base, TimestampMixin):
    """Target root path configuration for a workspace.

    Defines named URI prefixes where task outputs are stored.
    These are shared across all users in a workspace to ensure consistent
    task output locations (e.g., S3 bucket paths, local directories).

    The `name` is unique within a workspace and is used by tasks to reference
    which storage location to use.
    """

    __tablename__ = "target_roots"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_target_root_workspace_name"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=generate_uuid,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    uri_prefix: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
    )

    # Relationships
    workspace: Mapped[Workspace] = relationship(back_populates="target_roots")
