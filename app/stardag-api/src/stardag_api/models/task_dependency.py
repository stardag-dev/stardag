"""TaskDependency model for graph edges."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid7

if TYPE_CHECKING:
    from stardag_api.models.task import Task


# Width of ``scope_key`` on edges and builds. A structure scope is
# ``<code_id>:<config_hash>`` — a full git SHA (40) or a UUID hex (32), a
# colon, and a 16-hex config hash — or ``build:<uuid>`` for a build that
# registered without one. 96 leaves room for either to grow.
SCOPE_KEY_LENGTH = 96


class TaskDependency(Base, TimestampMixin):
    """Graph edges representing task dependencies, **per structure scope**.

    upstream_task_id -> downstream_task_id means:
    "downstream depends on upstream" or "upstream must complete before downstream"

    An edge is a fact about the *code* that declared or discovered it, not
    about the task id, so every edge carries the ``scope_key`` of the build
    that registered it — the code version plus the structure-significant
    build config. Readiness is evaluated over the edges in a build's own
    scope only; edges in another scope are invisible to it. Within a scope
    edges only ever grow, which is what makes gating sound: it can
    over-approximate but never under-approximate. See
    ``docs/design/scope-keyed-dependency-structure.md``.

    ``scope_key IS NULL`` marks rows written before scopes existed. They are
    kept as history for the graph view, where they count for every node
    (the pre-scope behaviour), and gate nothing.

    Supports efficient graph traversal queries for:
    - Finding all upstream dependencies (what does this task depend on?)
    - Finding all downstream dependents (what depends on this task?)
    - Full DAG visualization
    """

    __tablename__ = "task_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "scope_key",
            "upstream_task_id",
            "downstream_task_id",
            name="uq_task_dependency_scope_edge",
        ),
        Index("ix_task_dep_upstream", "upstream_task_id"),
        Index("ix_task_dep_downstream", "downstream_task_id"),
        # The gating probe: "edges INTO this task, in THIS scope". Narrower
        # than the downstream-only index above by the scope prefix, which
        # is the one that gets read on every frontier poll.
        Index("ix_task_dep_scope_downstream", "scope_key", "downstream_task_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=generate_uuid7,
    )

    upstream_task_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    downstream_task_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The structure scope this edge belongs to — ``Build.scope_key`` of the
    # build that registered it. Nullable only for rows predating scopes.
    scope_key: Mapped[str | None] = mapped_column(
        String(SCOPE_KEY_LENGTH),
        nullable=True,
    )

    # True when this edge was added at runtime because the downstream task
    # yielded the upstream as a dynamic dep. False for edges coming from a
    # task's static ``requires()`` at registration time. An edge that exists
    # as both static and dynamic (unusual but possible) is stored once with
    # the FIRST observation; we don't flip from False -> True on later writes
    # because the initial registration is authoritative.
    is_dynamic: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
        default=False,
    )

    # Relationships
    upstream_task: Mapped[Task] = relationship(
        foreign_keys=[upstream_task_id],
        back_populates="downstream_edges",
    )
    downstream_task: Mapped[Task] = relationship(
        foreign_keys=[downstream_task_id],
        back_populates="upstream_edges",
    )
