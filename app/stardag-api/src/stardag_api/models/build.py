"""``build``: one request to materialise a set of root tasks.

A build is a request, not an owner. Its structure lives on its plans (one
per scope it has been planned under, one of them active); the build row
keeps the request (``root_task_ids``, at completion-id level, stable across
rollover), the stored status, and the reactive-scheduling columns.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import (
    Base,
    EnvironmentScopedMixin,
    generate_uuid7,
    pg_enum,
    utc_now,
)
from stardag_api.models.enums import BuildStatus

if TYPE_CHECKING:
    from stardag_api.models.user import User
    from stardag_api.models.environment import Environment


class Build(EnvironmentScopedMixin, Base):
    """Represents one ``sd.build()`` request for a set of root tasks.

    Status is a stored column driven by build events; the server does not
    flip it inside task transactions. The active plan is found through
    ``plan``, not stored here twice.
    """

    __tablename__ = "build"
    __table_args__ = (
        # FK target of every composite FK onto ``build`` (the environment rule).
        UniqueConstraint("environment_id", "id", name="uq_build_environment_id"),
        Index("ix_build_environment_created", "environment_id", "created_at"),
        Index(
            "ix_build_environment_last_active",
            "environment_id",
            "last_active_at",
        ),
        # Serves ``GET /builds?status=`` — "builds in THIS environment with
        # status X, most recently active first" — and the reaper's
        # "RUNNING builds, stalest first". Same shape, and the same reasoning,
        # as ix_task_environment_status: the single-environment filter and
        # the status filter are useless apart (RUNNING spans every tenant;
        # the environment-keyed composites don't mention status), and the
        # distribution is badly skewed — a mature environment is almost all
        # terminal builds with a handful RUNNING.
        #
        # ``last_active_at`` is the third column, not a separate index, so
        # the ORDER BY of a single-status page resolves from the index alone
        # in either direction (newest-first for the default listing,
        # stalest-first for a staleness query).
        Index(
            "ix_build_environment_status",
            "environment_id",
            "status",
            "last_active_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=generate_uuid7,
    )
    user_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Memorable slug name (e.g., "brave-tiger-42")
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Optional user-provided documentation
    description: Mapped[str | None] = mapped_column(Text)

    # The request at completion-id level: the ``task_id`` hashes of the
    # roots, stable across rollover. A rollover whose re-read roots hash
    # differently fails the build ("re-trigger it as a new build").
    root_task_ids: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )

    # Bumped on build-level lifecycle events (BUILD_RESUMED, BUILD_COMPLETED,
    # BUILD_FAILED, BUILD_CANCELLED, BUILD_EXIT_EARLY; initial creation sets
    # it via DEFAULT) and, as in v1, on task activity: every status change of
    # a task the build's active plan holds bumps it too
    # (``wakeups.flag_after_transition``, called from ``transition_task()``).
    # That bump is a best-effort ``SKIP LOCKED`` UPDATE outside this
    # service — a build whose row a claiming start or a terminal transition
    # holds at that moment just misses the one bump, caught by the build's
    # next task event or its own next lifecycle write. So the per-task hot
    # path is not free of contention on the build row, but it never waits
    # for it.
    #
    # This column drives the "Home" / list-builds ordering and the
    # ``idle_for_seconds`` filter (``GET /builds``): "idle" means no task or
    # lifecycle activity for that long, not merely no lifecycle change.
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    # Executor-descriptive metadata of the trigger that created (or most
    # recently resumed-with-metadata) the build, e.g. {"kind": "modal",
    # "app_name": ..., "workspace": ..., "environment": ...,
    # "function_name": ..., "reactive": ...}. Set from BuildCreate /
    # the resume endpoint; kept (not cleared) on resumes that don't carry
    # metadata — the in-container SDK resume of a Modal-triggered build
    # doesn't know its trigger metadata.
    executor_metadata: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    # The wake-up flags (``needs_tick_at``, ``tick_requested_at``) live on
    # ``build_wake``, one row per build: flagging must not lock this row,
    # which a claiming start holds ``FOR SHARE`` (models/build_wake.py).

    # The reactive scheduler's single-flight lease on this build: at most
    # one tick drives a build at a time. Held while a tick runs (renewed
    # while it lingers), cleared on exit, and honoured only until
    # ``scheduler_lease_until`` — a tick whose container died leaves the
    # column set, and treating that as a live scheduler would suppress
    # wake-ups for exactly the build that most needs them.
    #
    scheduler_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Who holds it. Renew and release are owner-checked, so a tick cannot
    # renew or drop a lease that was taken over after its own lapsed.
    scheduler_lease_owner: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    # Reactive-scheduling owner: the name of the app whose scheduler ticks
    # drive this build, set by PUT /builds/{id}/reactive-meta. NULL means
    # the build is NOT reactively scheduled — its presence
    # (``reactive_app_name IS NOT NULL``) is the "this build is driven by
    # scheduler ticks" marker (a stray tick no-ops on a build with NULL
    # here, so a resident-orchestrator build is never double-scheduled). The
    # owning app drives the ticks (ownership guard). A typed column (not
    # JSONB) so the watchdog's real query — "list RUNNING reactive builds
    # owned by app X" — is a server-side filter (see GET /builds).
    reactive_app_name: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )

    # Reactive-scheduler tick configuration (a ``TickConfig`` kwargs dict,
    # SDK-owned and evolving with it — hence JSONB, not typed columns).
    # Read by every tick (surfaced on the build frontier) so worker
    # wake-ups and watchdog sweeps — which spawn with only the build id —
    # share the trigger-time config. NULL/absent is treated as ``{}``. Only
    # meaningful when reactive_app_name is set. Kept off the target root
    # (the asset store, which may be immutable) because a re-trigger must be
    # able to update it.
    reactive_tick_kwargs: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    # ------------------------------------------------------------------
    # Stored build status, driven by build events (v1's ``latest_*``
    # columns, renamed to match ``task``). ``/complete`` recomputes the
    # plan-complete predicate in its own transaction before it moves this.
    # ------------------------------------------------------------------
    status: Mapped[BuildStatus] = mapped_column(
        pg_enum(BuildStatus, "build_status"),
        nullable=False,
        default=BuildStatus.PENDING,
        server_default=BuildStatus.PENDING.value,
    )
    # First BUILD_STARTED. A resume does *not* move it; resuming is flagged
    # by ``is_resumed``.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The terminal event that produced ``status``; cleared by a resume.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ``external_id`` of the user who triggered the current status, when it
    # came from a manual override. NULL for machine-driven transitions.
    status_triggered_by_user_id: Mapped[str | None] = mapped_column(String(255))
    # Why the build is FAILED: the message of the BUILD_FAILED that produced
    # the current status. NULL for every other status, so a build resumed or
    # completed after a failure does not keep explaining it.
    error_message: Mapped[str | None] = mapped_column(Text)
    # True iff the event that produced the current status was BUILD_RESUMED.
    is_resumed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    # Relationships
    environment: Mapped[Environment] = relationship(back_populates="builds")
    user: Mapped[User | None] = relationship(back_populates="builds")
