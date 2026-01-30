"""Environment model for isolated environments."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid7

if TYPE_CHECKING:
    from stardag_api.models.api_key import ApiKey
    from stardag_api.models.build import Build
    from stardag_api.models.workspace import Workspace
    from stardag_api.models.target_root import TargetRoot
    from stardag_api.models.task import Task
    from stardag_api.models.user import User


class Environment(Base, TimestampMixin):
    """Isolated environment within a workspace.

    Similar to a 'project' - contains builds, tasks, and their relationships.
    """

    __tablename__ = "environments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug", name="uq_environment_workspace_slug"),
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
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    # Personal environment owner (null for shared environments)
    owner_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Maximum concurrent locks allowed for this environment (null = unlimited)
    max_concurrent_locks: Mapped[int | None] = mapped_column(
        nullable=True,
        default=None,
    )

    # Relationships
    workspace: Mapped[Workspace] = relationship(back_populates="environments")
    owner: Mapped[User | None] = relationship()
    builds: Mapped[list[Build]] = relationship(
        back_populates="environment",
        cascade="all, delete-orphan",
    )
    tasks: Mapped[list[Task]] = relationship(
        back_populates="environment",
        cascade="all, delete-orphan",
    )
    api_keys: Mapped[list[ApiKey]] = relationship(
        back_populates="environment",
        cascade="all, delete-orphan",
    )
    target_roots: Mapped[list[TargetRoot]] = relationship(
        back_populates="environment",
        cascade="all, delete-orphan",
    )
