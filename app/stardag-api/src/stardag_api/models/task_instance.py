"""``task_instance`` and ``task_instance_dependency``: structure, per scope.

An **instance** is a registry row: a task as constructed under one scope
``(deployment_id, settings_hash)``. (The Python object is a *task object*;
one task object under two deployments is two instances.) Edges belong to
instances, never to plans, and are never deleted. ``instance_hash`` is not
a public identifier on its own: an instance is addressed by its row id or
by the full scope triple.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from stardag_api.models.base import Base, EnvironmentScopedMixin, generate_uuid7


class TaskInstance(EnvironmentScopedMixin, Base):
    """A task as constructed under a scope.

    Per scope: ``instance_hash <-> body`` is 1:1 (an insert that hits the
    unique key with a different body is 409 ``instance_body_conflict``),
    ``instance_hash -> task`` is a function, and once ``expanded_at`` is set
    so is ``instance_hash -> upstream instance set``. Many instances per
    ``task_pk`` per scope are allowed: different constructions of one
    promise.
    """

    __tablename__ = "task_instance"
    __table_args__ = (
        UniqueConstraint(
            "deployment_id",
            "settings_hash",
            "instance_hash",
            name="uq_task_instance_scope_hash",
        ),
        # FK target: a member's / execution's instance realises its task.
        UniqueConstraint(
            "environment_id", "id", "task_pk", name="uq_task_instance_id_task"
        ),
        # FK target: an edge's / member's instance is in the given scope.
        UniqueConstraint(
            "environment_id",
            "id",
            "deployment_id",
            "settings_hash",
            name="uq_task_instance_id_scope",
        ),
        ForeignKeyConstraint(
            ["environment_id", "deployment_id"],
            ["deployment.environment_id", "deployment.id"],
            name="fk_task_instance_deployment",
            ondelete="NO ACTION",
        ),
        ForeignKeyConstraint(
            ["environment_id", "settings_hash"],
            ["settings.environment_id", "settings.hash"],
            name="fk_task_instance_settings",
        ),
        ForeignKeyConstraint(
            ["environment_id", "task_pk"],
            ["task.environment_id", "task.id"],
            name="fk_task_instance_task",
        ),
        Index(
            "ix_task_instance_scope_task",
            "deployment_id",
            "settings_hash",
            "task_pk",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=generate_uuid7)
    # The scope.
    deployment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    settings_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Hash of the canonical body (all parameters), computed by the SDK.
    instance_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # The completion this instance realises.
    task_pk: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    # All parameters, registry-mode dump; nested tasks as full dumps.
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # The closure flag: set when this instance's ``requires()`` was evaluated
    # under its scope and every resulting edge recorded (zero edges
    # included). NULL means "not yet looked for" — a discovery job, unless
    # the task is COMPLETED (pruned at discovery).
    expanded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskInstanceDependency(EnvironmentScopedMixin, Base):
    """An edge ``upstream -> downstream`` between two instances of one scope.

    The scope is denormalised onto the edge so that "both ends share a
    scope" is a constraint (two composite FKs), not a check. Within a scope
    edges only grow; they belong to no plan and are never deleted.
    """

    __tablename__ = "task_instance_dependency"
    __table_args__ = (
        PrimaryKeyConstraint(
            "downstream_instance_id",
            "upstream_instance_id",
            name="pk_task_instance_dependency",
        ),
        ForeignKeyConstraint(
            [
                "environment_id",
                "downstream_instance_id",
                "deployment_id",
                "settings_hash",
            ],
            [
                "task_instance.environment_id",
                "task_instance.id",
                "task_instance.deployment_id",
                "task_instance.settings_hash",
            ],
            name="fk_task_instance_dependency_downstream",
        ),
        ForeignKeyConstraint(
            [
                "environment_id",
                "upstream_instance_id",
                "deployment_id",
                "settings_hash",
            ],
            [
                "task_instance.environment_id",
                "task_instance.id",
                "task_instance.deployment_id",
                "task_instance.settings_hash",
            ],
            name="fk_task_instance_dependency_upstream",
        ),
        # Reverse traversal (downstream-of, skip-blocked, exclusion cascade);
        # the primary key serves the forward direction.
        Index("ix_task_instance_dependency_upstream", "upstream_instance_id"),
    )

    downstream_instance_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    upstream_instance_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    deployment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    settings_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Set at first insert (a yielded edge), never changed.
    is_dynamic: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
