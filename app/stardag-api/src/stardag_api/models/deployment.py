"""Deployment records: which code versions of an app have been deployed.

A deployment here is exactly the execution backend's: one code version of
one app name. The backend keeps one live per name — a redeploy replaces it,
in-flight inputs finish on the old version and every new spawn lands on the
new one — and a running build **follows** it: the first scheduler pass on
new code re-plans the build under its own structure scope. So nothing is
kept alive beside the current deployment and nothing needs collecting;
the record is provenance ("which code was live when") and the answer to
"what is current", which is simply the newest row for the app.

See ``docs/design/scope-keyed-dependency-structure.md``, "Rollover".
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid7, utc_now

if TYPE_CHECKING:
    from stardag_api.models.environment import Environment


class Deployment(Base, TimestampMixin):
    """One deployed code version of one app, in one environment."""

    __tablename__ = "deployments"
    __table_args__ = (
        UniqueConstraint(
            "environment_id", "app_name", "code_id", name="uq_deployment_app_code"
        ),
        # The listing's question: the deployments of an app, newest first.
        Index(
            "ix_deployments_environment_app_deployed",
            "environment_id",
            "app_name",
            "deployed_at",
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
    # The app name as written and as deployed: what ``reactive_app_name``
    # on a build holds, and what ticks and workers are spawned on.
    app_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # The code identity baked into the deployment: a full git SHA for a
    # clean tree, a UUID hex for a dirty one. The first component of every
    # scope key a build planned on this deployment gets.
    code_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # When this code was (last) deployed. Refreshed when the same code is
    # deployed again; the newest row per app is the current deployment.
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    # The backend's own identifier for this deployment, when the deploy knew
    # it: Modal's app id (``ap-...``). Refreshed on re-record, so the UI can
    # link to the deployment the backend is actually running.
    modal_app_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    environment: Mapped[Environment] = relationship()
