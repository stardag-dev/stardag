"""``deployment``: one deployed (or locally derived) code version of an app.

Half of the deterministic scope ``(deployment_id, settings_hash)``. A Modal
deployment is created by ``POST /deployments`` **before** the deploy (the
server assigns ``generation``) and activated after it succeeded; a
``local`` one is looked up or created by ``(environment, kind, code_id)``
and born activated. "Current" for an app is the **activated row with the
highest generation**. Referenced ``ON DELETE RESTRICT`` from everywhere, so
retention of a retired deployment's instances has to be explicit.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from stardag_api.models.base import Base, EnvironmentScopedMixin, pg_enum
from stardag_api.models.enums import DeploymentKind


class Deployment(EnvironmentScopedMixin, Base):
    """One deployment: a scope half, and the answer to "what is current"."""

    __tablename__ = "deployment"
    __table_args__ = (
        UniqueConstraint("environment_id", "id", name="uq_deployment_environment_id"),
        UniqueConstraint(
            "environment_id",
            "kind",
            "app_name",
            "generation",
            name="uq_deployment_app_generation",
        ),
        Index(
            "ix_deployment_app_generation_desc",
            "environment_id",
            "kind",
            "app_name",
            text("generation DESC"),
        ),
        # A local deployment is derived from its code id: one per code id.
        Index(
            "uq_deployment_local_code_id",
            "environment_id",
            "code_id",
            unique=True,
            postgresql_where=text("kind = 'local'"),
        ),
    )

    # Client-minted (uuid7), baked into the image as STARDAG_DEPLOYMENT_ID.
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    kind: Mapped[DeploymentKind] = mapped_column(
        pg_enum(DeploymentKind, "deployment_kind"), nullable=False
    )
    app_name: Mapped[str] = mapped_column(String(64), nullable=False)
    code_id: Mapped[str] = mapped_column(String(64), nullable=False)
    image_id: Mapped[str | None] = mapped_column(String(128))
    modal_app_id: Mapped[str | None] = mapped_column(String(64))
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Server-assigned at create, monotonic per (environment, kind, app_name).
    # Order is fixed when a deploy *starts*, so a late record cannot roll a
    # build back to older code.
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    # Set by /activate after the deploy succeeded (by the insert, for a
    # local row). NULL rows are never current and cannot host a plan.
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
