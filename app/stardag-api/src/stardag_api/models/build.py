"""Build model for tracking DAG executions."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid7, utc_now
from stardag_api.models.enums import BuildStatus

if TYPE_CHECKING:
    from stardag_api.models.event import Event
    from stardag_api.models.user import User
    from stardag_api.models.environment import Environment


class Build(Base, TimestampMixin):
    """Represents execution of sd.build() for a DAG/set of tasks.

    Status is a fold of the build's build-level events, denormalised onto
    the row (the ``latest_*`` columns below) exactly as ``Task`` does it.
    """

    __tablename__ = "builds"
    __table_args__ = (
        Index("ix_builds_environment_created", "environment_id", "created_at"),
        Index(
            "ix_builds_environment_last_active",
            "environment_id",
            "last_active_at",
        ),
        # Serves ``GET /builds?status=`` — "builds in THIS environment with
        # status X, most recently active first" — and the reaper's
        # "RUNNING builds, stalest first". Same shape, and the same reasoning,
        # as ix_tasks_environment_status: the single-environment filter and
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
            "ix_builds_environment_status",
            "environment_id",
            "latest_status",
            "last_active_at",
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

    # Git context
    commit_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    # Root task IDs (the tasks passed to sd.build()).
    # JSONB on Postgres for consistency and to avoid reparsing on access.
    root_task_ids: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=list,
    )

    # Bumped on build-level lifecycle events only (BUILD_RESUMED,
    # BUILD_COMPLETED, BUILD_FAILED, BUILD_CANCELLED, BUILD_EXIT_EARLY) —
    # initial creation sets it via DEFAULT. Task events do NOT touch this
    # column, so the per-task hot path is free of contention on the build
    # row.
    #
    # This column drives the "Home" / list-builds ordering: a resumed
    # build (BUILD_RESUMED) jumps to the top instead of staying buried at
    # its original ``created_at`` position. The trade-off vs touching on
    # every task event is that a long-running build won't bump position
    # while it's mid-execution — but its ``status=running`` badge already
    # signals activity, and "most recent lifecycle change" is a cleaner
    # sort key than "any event in the build's subtree." See
    # ``_touch_build_last_active`` in ``routes/builds.py``.
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
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
    )

    # Reactive-scheduler dirty flag: set by POST /builds/{id}/notify (e.g. a
    # worker finishing a task), cleared by the scheduler tick before it
    # computes the frontier (DELETE /builds/{id}/notify). A notify landing
    # between clear and compute re-sets it, so the tick's linger poll picks
    # the wake-up back up — no lost signals. NULL = no pending wake-up.
    needs_tick_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # When a caller was last told to spawn a tick for this build — by
    # ``POST /builds/wake-candidates`` handing it out, or by
    # ``POST /builds/{id}/notify`` reporting no live scheduler to a worker
    # that will spawn. A flagged build is handed out at most once per
    # ``services.wakeups.WAKE_HANDOUT_WINDOW``, which is what turns N
    # concurrent askers into one container rather than N. Not a liveness
    # signal and not cleared: it simply ages out.
    tick_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # The reactive scheduler's single-flight lease on this build: at most
    # one tick drives a build at a time. Held while a tick runs (renewed
    # while it lingers), cleared on exit, and honoured only until
    # ``scheduler_lease_until`` — a tick whose container died leaves the
    # column set, and treating that as a live scheduler would suppress
    # wake-ups for exactly the build that most needs them.
    #
    # On the build row rather than in ``distributed_locks`` because every
    # reader wants it alongside the build: ``is_scheduler_live`` and
    # ``select_wake_candidates`` were both a second table and a lock name
    # assembled from a build id, and the name prefix had to be kept
    # byte-identical in the SDK and the API by comment alone.
    #
    # Transient by nature, so the migration backfills nothing: a lease that
    # existed across the deploy is simply not seen, which costs at most one
    # duplicate tick (idempotent, and arbitrated per task by the claim).
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
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
    )

    # ------------------------------------------------------------------
    # Denormalised build-status columns.
    #
    # Maintained in-transaction whenever a build-level event is created
    # (see services.status.apply_event_to_build, called by every build
    # lifecycle path). They hold exactly what the historical
    # ``get_build_status`` event replay returned, so a read is a column
    # read rather than a scan of the build's whole event stream.
    #
    # They exist for three reasons, in increasing order of importance:
    # every BuildResponse used to cost one event scan; ``GET /builds?status=``
    # could not filter in SQL and so scanned a bounded window of candidates
    # and reported a `total` that was only the matches *within that window*;
    # and the stale-build reaper needed an exact, unbounded "is this build
    # RUNNING?", which meant a second SQL encoding of the same rule that
    # disagreed with the replay on timestamp ties. One column, one answer.
    # ------------------------------------------------------------------
    latest_status: Mapped[BuildStatus] = mapped_column(
        String(32),
        nullable=False,
        default=BuildStatus.PENDING,
    )
    # First BUILD_STARTED. A resume does *not* move it — the build started
    # when it first started; resuming is a separate concept, flagged by
    # ``latest_is_resumed``.
    latest_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    # The terminal event that produced ``latest_status``; cleared by a
    # resume so the UI doesn't keep showing a stale "completed at".
    latest_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    # ``external_id`` of the user who triggered the current status, when it
    # came from a manual UI override (the ``triggered_by_user_id`` key in
    # the event metadata). NULL for the machine-driven transitions —
    # start/resume/exit-early are never user-triggered.
    latest_status_triggered_by_user_id: Mapped[str | None] = mapped_column(String(255))
    # True iff the event that produced the current status was BUILD_RESUMED,
    # i.e. the build was picked up again after finishing/failing and is
    # RUNNING under resume semantics. The UI surfaces this as
    # "running (resumed)".
    latest_is_resumed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # Relationships
    environment: Mapped[Environment] = relationship(back_populates="builds")
    user: Mapped[User | None] = relationship(back_populates="builds")
    events: Mapped[list[Event]] = relationship(
        back_populates="build",
        cascade="all, delete-orphan",
        order_by="Event.created_at",
    )
