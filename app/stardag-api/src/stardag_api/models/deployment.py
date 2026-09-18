"""Deployment records: which code is deployed under which handle.

A stardag app has a *family* name the user writes (``StardagApp("myapp")``)
and one concrete *handle* per deployed code version (``myapp--<code_id>``).
The handle exists because the execution backend has one live deployment per
app name and no addressable versions: to keep a running build on the code
it started with while newer code deploys beside it, the version has to be
in the name. That is a placeholder for a capability the backend lacks, and
two rules keep it swappable — only the SDK's resolver knows the naming
convention, and **this record, not the app name, is the identity**. "Which
deployments of this family are live" and "which have no running build" are
answered here.

See ``docs/design/scope-keyed-dependency-structure.md``, "Deployments".
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid7

if TYPE_CHECKING:
    from stardag_api.models.environment import Environment


class Deployment(Base, TimestampMixin):
    """One deployed code version of an app family, in one environment."""

    __tablename__ = "deployments"
    __table_args__ = (
        UniqueConstraint("environment_id", "handle", name="uq_deployment_handle"),
        # The resolver's question: newest live deployment of a family.
        Index(
            "ix_deployments_environment_family_created",
            "environment_id",
            "family",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=generate_uuid7,
    )
    environment_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # What the user wrote: the app name before any version suffix.
    family: Mapped[str] = mapped_column(String(64), nullable=False)
    # What the backend knows the deployment as; what ``reactive_app_name``
    # on a build holds, and what ticks and workers are spawned on.
    handle: Mapped[str] = mapped_column(String(64), nullable=False)
    # The code identity baked into the deployment: a full git SHA for a
    # clean tree, a UUID hex for a dirty one. The first component of every
    # scope key a build on this deployment gets.
    code_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Set when the deployment is retired (its app stopped). A retired
    # deployment is excluded from resolution; its record stays so the
    # provenance of builds that ran on it can still be read.
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    environment: Mapped[Environment] = relationship()
