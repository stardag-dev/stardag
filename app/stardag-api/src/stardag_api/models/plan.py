"""``plan`` and ``plan_member``: one request, under one scope, and its members.

A build has one or more plans, one per scope it has been planned under, and
exactly one **active** plan. Membership is a relation (not an event scan):
one instance per completion per plan, enforced by the primary key.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    UniqueConstraint,
    Uuid,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from stardag_api.models.base import Base, EnvironmentScopedMixin, pg_enum
from stardag_api.models.enums import AdmittedBy, ExclusionReason


class Plan(EnvironmentScopedMixin, Base):
    """A build's request under one scope ``(deployment_id, settings_hash)``.

    Lifecycle timestamps: ``activated_at`` (the build's active plan from
    here; the first plan activates on create, a replacement on seal),
    ``sealed_at`` (the static phase is fully stated and verified), and
    ``superseded_at`` (a replacement activated). Build completion requires
    ``sealed_at``.
    """

    __tablename__ = "plan"
    __table_args__ = (
        UniqueConstraint(
            "build_id", "deployment_id", "settings_hash", name="uq_plan_build_scope"
        ),
        UniqueConstraint("build_id", "generation", name="uq_plan_build_generation"),
        # FK target: event.plan_id.
        UniqueConstraint("environment_id", "id", name="uq_plan_environment_id"),
        # FK target: a member is in its plan's scope.
        UniqueConstraint(
            "environment_id",
            "id",
            "deployment_id",
            "settings_hash",
            name="uq_plan_id_scope",
        ),
        # Exactly one active plan per build, while a replacement can be
        # registered alongside it.
        Index(
            "uq_plan_build_active",
            "build_id",
            unique=True,
            postgresql_where=text("activated_at IS NOT NULL AND superseded_at IS NULL"),
        ),
        ForeignKeyConstraint(
            ["environment_id", "build_id"],
            ["build.environment_id", "build.id"],
            name="fk_plan_build",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["environment_id", "deployment_id"],
            ["deployment.environment_id", "deployment.id"],
            name="fk_plan_deployment",
            ondelete="NO ACTION",
        ),
        ForeignKeyConstraint(
            ["environment_id", "settings_hash"],
            ["settings.environment_id", "settings.hash"],
            name="fk_plan_settings",
        ),
    )

    # Client-minted (idempotent create).
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    build_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    deployment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    settings_hash: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    # Server-assigned per build at create, monotonic. /seal activates a plan
    # only if no higher-generation plan exists for the build.
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlanMember(EnvironmentScopedMixin, Base):
    """A completion in a plan, and the one instance that realises it there.

    The scope is denormalised onto the row so that "a member's instance is
    in its plan's scope" is two composite FKs, not a check. No counters:
    attempts and interruptions are counted from ``execution`` rows.
    """

    __tablename__ = "plan_member"
    __table_args__ = (
        # The one-instance-per-completion-per-plan rule.
        PrimaryKeyConstraint("plan_id", "task_pk", name="pk_plan_member"),
        # FK target: task.claim_plan_id (the environment-carrying form of the
        # primary key).
        UniqueConstraint(
            "environment_id", "plan_id", "task_pk", name="uq_plan_member_task"
        ),
        # FK target: execution. Follows from the primary key, since an
        # instance realises exactly one task.
        UniqueConstraint(
            "environment_id",
            "plan_id",
            "instance_id",
            name="uq_plan_member_instance",
        ),
        CheckConstraint(
            "(excluded_at IS NULL) = (excluded_reason IS NULL)",
            name="ck_plan_member_exclusion_has_reason",
        ),
        ForeignKeyConstraint(
            ["environment_id", "task_pk"],
            ["task.environment_id", "task.id"],
            name="fk_plan_member_task",
        ),
        ForeignKeyConstraint(
            ["environment_id", "instance_id", "task_pk"],
            [
                "task_instance.environment_id",
                "task_instance.id",
                "task_instance.task_pk",
            ],
            name="fk_plan_member_instance_task",
        ),
        ForeignKeyConstraint(
            ["environment_id", "plan_id", "deployment_id", "settings_hash"],
            [
                "plan.environment_id",
                "plan.id",
                "plan.deployment_id",
                "plan.settings_hash",
            ],
            name="fk_plan_member_plan_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["environment_id", "instance_id", "deployment_id", "settings_hash"],
            [
                "task_instance.environment_id",
                "task_instance.id",
                "task_instance.deployment_id",
                "task_instance.settings_hash",
            ],
            name="fk_plan_member_instance_scope",
        ),
        # "Plans holding this task": wake-up flagging over membership.
        Index("ix_plan_member_task", "task_pk"),
    )

    plan_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    task_pk: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    instance_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    deployment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    settings_hash: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    # The build's request, as instances of this plan.
    is_root: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    admitted_by: Mapped[AdmittedBy] = mapped_column(
        pg_enum(AdmittedBy, "plan_member_admitted_by"), nullable=False
    )
    # "Given up on" (STA-104): not scheduled, does not gate the build's
    # completion. Cascades to the member's downstream closure within the
    # plan; an excluded root fails the build.
    excluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    excluded_reason: Mapped[ExclusionReason | None] = mapped_column(
        pg_enum(ExclusionReason, "plan_member_exclusion_reason")
    )
