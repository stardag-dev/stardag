"""denormalised build status columns with backfill

Revision ID: e87efdab31ca
Revises: f6a5873c30fb
Create Date: 2026-08-07 23:55:11.594802

Adds five ``latest_*`` columns on ``builds`` holding exactly what the
``get_build_status`` event replay used to return, then backfills them by
replaying every existing build-level event in order.

Motivation: build status was not a column, so every ``BuildResponse`` cost a
scan of the build's event stream, ``GET /builds?status=`` could not filter in
SQL (it scanned a bounded window of candidates and reported the matches
*within that window* as ``total``), and the stale-build reaper needed a
second, independent SQL encoding of the same rule to get an exact answer.
The columns are kept up to date in-transaction whenever a build-level event
is created (see ``services.status.apply_event_to_build``).

Backfill strategy: one Python loop over build-level events (``task_id IS
NULL``) ordered ``created_at ASC, id ASC``, applying the same rules as the
in-process ``apply_event_to_build``. This reproduces what ``get_build_status``
answered for every build, so no build changes status across the migration.

The ``id`` tiebreaker matters for exactly one case: two build-level events of
the same build sharing a ``created_at``. ``get_build_status`` ordered on the
timestamp alone and let the index decide; ``id`` is a UUID7, so ordering by
it is ordering by creation — the same arrival order the live fold applies
events in. A tie therefore resolves the same way before and after the
migration, which the timestamp alone could not guarantee.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e87efdab31ca"
down_revision: Union[str, Sequence[str], None] = "f6a5873c30fb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ----- Backfill logic (mirrors services.status.apply_event_to_build)

_TERMINAL_BUILD_STATUS = {
    "build_completed": "completed",
    "build_failed": "failed",
    "build_cancelled": "cancelled",
    "build_exit_early": "exit_early",
}
# build_exit_early is excluded: the SDK emits it, never a user.
_USER_TRIGGERABLE = ("build_completed", "build_failed", "build_cancelled")


_UPDATE_BATCH = 500


def _fresh_state() -> dict:
    return {
        "status": "pending",
        "started_at": None,
        "completed_at": None,
        "triggered_by": None,
        "is_resumed": False,
    }


def _backfill_build_status(connection: sa.engine.Connection) -> None:
    """Replay every build-level event into the latest_* columns on its build.

    Streamed per build rather than accumulated: this runs at upgrade time on
    installations whose event tables are the biggest thing they own, and a
    dict keyed by every build that ever existed is a poor thing to require
    of the machine running the migration.

    Ordering by ``build_id`` first is what makes that possible, and is safe
    because the replay only ever depends on the order of events *within* one
    build — the global interleaving carries no information here. When the
    build id changes, the previous build's state is final and can be
    flushed.
    """
    update_stmt = sa.text(
        """
        UPDATE builds
        SET latest_status = :status,
            latest_started_at = :started_at,
            latest_completed_at = :completed_at,
            latest_status_triggered_by_user_id = :triggered_by,
            latest_is_resumed = :is_resumed
        WHERE id = :build_id
        """
    )
    pending_updates: list[dict] = []

    def flush(force: bool = False) -> None:
        if pending_updates and (force or len(pending_updates) >= _UPDATE_BATCH):
            connection.execute(update_stmt, pending_updates)
            pending_updates.clear()

    # Statement-level, NOT `connection.execution_options(...)`: that mutates
    # the connection Alembic is running the whole migration on, and a
    # streaming connection reports rowcount -1 — which Alembic reads as "the
    # version-table update matched no row" and aborts every upgrade.
    events = connection.execute(
        sa.text(
            """
            SELECT id, build_id, event_type, created_at, event_metadata
            FROM events
            WHERE task_id IS NULL
            ORDER BY build_id ASC, created_at ASC, id ASC
            """
        ).execution_options(stream_results=True)
    )

    current_id: str | None = None
    state = _fresh_state()

    for row in events:
        build_id = str(row.build_id)
        if build_id != current_id:
            if current_id is not None:
                pending_updates.append({"build_id": current_id, **state})
                flush()
            current_id = build_id
            state = _fresh_state()

        et = row.event_type
        meta = row.event_metadata or {}

        if et == "build_started":
            state.update(
                status="running",
                started_at=row.created_at,
                triggered_by=None,
                is_resumed=False,
            )
        elif et == "build_resumed":
            # Keeps started_at — the build started when it first started —
            # and clears completed_at so no stale "completed at" survives.
            state.update(
                status="running",
                completed_at=None,
                triggered_by=None,
                is_resumed=True,
            )
        elif et in _TERMINAL_BUILD_STATUS:
            state.update(
                status=_TERMINAL_BUILD_STATUS[et],
                completed_at=row.created_at,
                is_resumed=False,
                triggered_by=(
                    meta.get("triggered_by_user_id")
                    if et in _USER_TRIGGERABLE
                    else None
                ),
            )
        # Any other build-level event type is status-neutral.

    if current_id is not None:
        pending_updates.append({"build_id": current_id, **state})
    flush(force=True)


def upgrade() -> None:
    """Upgrade schema and backfill latest_* columns from build-level events."""
    # The two NOT NULL columns get a server_default so the ALTER succeeds on
    # existing rows; it is dropped again after the backfill so the
    # application owns the columns from then on. The defaults are also the
    # correct values for a build with no build-level events at all, which the
    # backfill loop never visits.
    op.add_column(
        "builds",
        sa.Column(
            "latest_status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "builds",
        sa.Column("latest_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "builds",
        sa.Column("latest_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "builds",
        sa.Column(
            "latest_status_triggered_by_user_id", sa.String(length=255), nullable=True
        ),
    )
    op.add_column(
        "builds",
        sa.Column(
            "latest_is_resumed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "ix_builds_environment_status",
        "builds",
        ["environment_id", "latest_status", "last_active_at"],
        unique=False,
    )

    _backfill_build_status(op.get_bind())

    op.alter_column("builds", "latest_status", server_default=None)
    op.alter_column("builds", "latest_is_resumed", server_default=None)


def downgrade() -> None:
    """Downgrade schema.

    Lossless: every column dropped here is derivable from the events table,
    which this migration never touched.
    """
    op.drop_index("ix_builds_environment_status", table_name="builds")
    op.drop_column("builds", "latest_is_resumed")
    op.drop_column("builds", "latest_status_triggered_by_user_id")
    op.drop_column("builds", "latest_completed_at")
    op.drop_column("builds", "latest_started_at")
    op.drop_column("builds", "latest_status")
