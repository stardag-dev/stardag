"""Task registry asset model for storing task registry assets."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    ForeignKey,
    Index,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from stardag_api.models.task import Task
    from stardag_api.models.workspace import Workspace


class TaskRegistryAsset(Base, TimestampMixin):
    """Task registry asset - stores rich outputs from completed tasks.

    Assets are associated with a specific task instance (by task_id hash)
    and can be markdown reports, JSON data, or other types.

    All asset bodies are stored as JSON:
    - For markdown: {"content": "<markdown string>"}
    - For json: the actual JSON data dict
    """

    __tablename__ = "task_registry_assets"
    __table_args__ = (
        UniqueConstraint(
            "task_pk",
            "asset_type",
            "name",
            name="uq_task_registry_asset_task_type_name",
        ),
        Index("ix_task_registry_assets_task_pk", "task_pk"),
        Index("ix_task_registry_assets_workspace", "workspace_id"),
    )

    # Auto-increment primary key
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Foreign key to tasks table (internal PK, not task_id hash)
    task_pk: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Workspace for access control
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Asset type discriminator (e.g., "markdown", "json")
    asset_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    # Asset name/slug for identification
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # Content - always stored as JSON
    # For markdown: {"content": "<markdown string>"}
    # For json: the actual JSON data dict
    body_json: Mapped[Any] = mapped_column(JSON, nullable=False)

    # Relationships
    task: Mapped[Task] = relationship(back_populates="registry_assets")
    workspace: Mapped[Workspace] = relationship()
