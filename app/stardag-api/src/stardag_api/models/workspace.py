"""Workspace model for isolated environments."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from stardag_api.models.api_key import ApiKey
    from stardag_api.models.build import Build
    from stardag_api.models.organization import Organization
    from stardag_api.models.target_root import TargetRoot
    from stardag_api.models.task import Task
    from stardag_api.models.user import User


class Workspace(Base, TimestampMixin):
    """Isolated environment within an organization.

    Similar to a 'project' - contains builds, tasks, and their relationships.
    """

    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("organization_id", "slug", name="uq_workspace_org_slug"),
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
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    # Personal workspace owner (null for shared workspaces)
    owner_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Maximum concurrent locks allowed for this workspace (null = unlimited)
    max_concurrent_locks: Mapped[int | None] = mapped_column(
        nullable=True,
        default=None,
    )

    # Relationships
    organization: Mapped[Organization] = relationship(back_populates="workspaces")
    owner: Mapped[User | None] = relationship()
    builds: Mapped[list[Build]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
    tasks: Mapped[list[Task]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
    api_keys: Mapped[list[ApiKey]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
    target_roots: Mapped[list[TargetRoot]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
