"""``task_artifact``: rich outputs of a completion (reports, JSON data).

Artifacts belong to the **promise** (``task``), not to an instance or a
build: any instance's execution that completes the task may write them.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKeyConstraint, Index, String, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from stardag_api.models.base import Base, EnvironmentScopedMixin, generate_uuid7


class TaskArtifact(EnvironmentScopedMixin, Base):
    """One named artifact of a task.

    All artifact bodies are stored as JSON:
    - For markdown: {"content": "<markdown string>"}
    - For json: the actual JSON data dict
    """

    __tablename__ = "task_artifact"
    __table_args__ = (
        UniqueConstraint(
            "task_pk",
            "artifact_type",
            "name",
            name="uq_task_artifact_task_type_name",
        ),
        ForeignKeyConstraint(
            ["environment_id", "task_pk"],
            ["task.environment_id", "task.id"],
            name="fk_task_artifact_task",
            ondelete="CASCADE",
        ),
        # "Artifacts in this environment, newest first".
        Index("ix_task_artifact_environment_created", "environment_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=generate_uuid7)
    task_pk: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    # Artifact type discriminator (e.g., "markdown", "json").
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # Artifact name/slug for identification.
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    body_json: Mapped[Any] = mapped_column(JSONB, nullable=False)
