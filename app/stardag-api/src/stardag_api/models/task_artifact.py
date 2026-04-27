"""Task artifact model for storing task artifacts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    ForeignKey,
    Index,
    JSON,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid7

if TYPE_CHECKING:
    from stardag_api.models.task import Task
    from stardag_api.models.environment import Environment


class TaskArtifact(Base, TimestampMixin):
    """Task artifact - stores rich outputs from completed tasks.

    Artifacts are associated with a specific task instance (by task_id hash)
    and can be markdown reports, JSON data, or other types.

    All artifact bodies are stored as JSON:
    - For markdown: {"content": "<markdown string>"}
    - For json: the actual JSON data dict
    """

    __tablename__ = "task_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "task_pk",
            "artifact_type",
            "name",
            name="uq_task_artifact_task_type_name",
        ),
        Index("ix_task_artifacts_task_pk", "task_pk"),
        Index("ix_task_artifacts_environment", "environment_id"),
        # Covers "list artifacts in env newest first" used by /tasks/keys and
        # /tasks/values, which previously had to sort an unindexed column.
        Index(
            "ix_task_artifacts_environment_created",
            "environment_id",
            "created_at",
        ),
    )

    # UUID7 primary key
    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=generate_uuid7,
    )

    # Foreign key to tasks table (internal PK, not task_id hash)
    task_pk: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Environment for access control
    environment_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Artifact type discriminator (e.g., "markdown", "json")
    artifact_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    # Artifact name/slug for identification
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # Content - always stored as JSON
    # For markdown: {"content": "<markdown string>"}
    # For json: the actual JSON data dict
    # JSONB on Postgres avoids reparsing on every access (search/Task Explorer
    # query inside this column); SQLite keeps JSON for tests.
    body_json: Mapped[Any] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
    )

    # Relationships
    task: Mapped[Task] = relationship(back_populates="artifacts")
    environment: Mapped[Environment] = relationship()
