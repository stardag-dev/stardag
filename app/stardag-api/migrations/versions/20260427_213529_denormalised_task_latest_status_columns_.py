"""denormalised task latest_status columns with backfill

Revision ID: 226b6ba1fa16
Revises: 7b8476ac0846
Create Date: 2026-04-27 21:35:29.710301

Adds nine ``latest_*`` columns on ``tasks`` that mirror the result of the
historical ``get_all_task_global_statuses`` event scan, then backfills them
by replaying every existing event in chronological order.

Motivation: the read-side functions previously scanned the entire events
table for every list/graph/search request. This denormalisation makes those
requests O(tasks_returned) instead of O(events_for_those_tasks). The columns
are kept up to date in-transaction whenever an event is created
(see ``services.status.apply_event_to_task``).

Backfill strategy: a single Python loop over events ordered by created_at
ascending, using the same priority logic as the in-process
``apply_event_to_task`` function. At observed prod sizes (events ~10k rows,
tasks ~2k rows) this completes in milliseconds.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "226b6ba1fa16"
down_revision: Union[str, Sequence[str], None] = "7b8476ac0846"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ----- Backfill priority logic (mirrors services.status.apply_event_to_task)


def _backfill_latest_status(connection: sa.engine.Connection) -> None:
    """Replay every event into the latest_* columns on its task."""
    # State per task while we replay.
    states: dict[str, dict] = {}

    events = connection.execute(
        sa.text(
            """
            SELECT id, build_id, task_id, event_type, created_at,
                   error_message, event_metadata
            FROM events
            WHERE task_id IS NOT NULL
            ORDER BY created_at ASC
            """
        )
    )

    for row in events:
        task_id = str(row.task_id)
        state = states.setdefault(
            task_id,
            {
                "status": "pending",
                "status_at": None,
                "status_event_id": None,
                "status_build_id": None,
                "started_at": None,
                "completed_at": None,
                "error_message": None,
                "waiting_for_lock": False,
                "commit_hash": None,
            },
        )

        et = row.event_type
        meta = row.event_metadata or {}
        event_commit = meta.get("commit_hash")

        if et == "task_completed":
            state.update(
                status="completed",
                status_at=row.created_at,
                status_event_id=row.id,
                status_build_id=row.build_id,
                completed_at=row.created_at,
                waiting_for_lock=False,
            )
            if event_commit is not None:
                state["commit_hash"] = event_commit
            continue

        if state["status"] == "completed":
            continue

        if et == "task_started":
            state.update(
                status="running",
                status_at=row.created_at,
                status_event_id=row.id,
                status_build_id=row.build_id,
                waiting_for_lock=False,
            )
            if state["started_at"] is None:
                state["started_at"] = row.created_at
            if event_commit is not None:
                state["commit_hash"] = event_commit
        elif et == "task_resumed":
            state.update(
                status="running",
                status_at=row.created_at,
                status_event_id=row.id,
                status_build_id=row.build_id,
                waiting_for_lock=False,
            )
            if event_commit is not None:
                state["commit_hash"] = event_commit
        elif et == "task_failed":
            state.update(
                status="failed",
                status_at=row.created_at,
                status_event_id=row.id,
                status_build_id=row.build_id,
                completed_at=row.created_at,
                error_message=row.error_message,
            )
            if event_commit is not None:
                state["commit_hash"] = event_commit
        elif et == "task_cancelled":
            state.update(
                status="cancelled",
                status_at=row.created_at,
                status_event_id=row.id,
                status_build_id=row.build_id,
                completed_at=row.created_at,
            )
            if event_commit is not None:
                state["commit_hash"] = event_commit
        elif et == "task_skipped":
            state.update(
                status="skipped",
                status_at=row.created_at,
                status_event_id=row.id,
                status_build_id=row.build_id,
                completed_at=row.created_at,
            )
        elif et == "task_suspended":
            state.update(
                status="suspended",
                status_at=row.created_at,
                status_event_id=row.id,
                status_build_id=row.build_id,
            )
        elif et == "task_waiting_for_lock":
            if state["status"] == "pending":
                state["waiting_for_lock"] = True
        elif et == "task_pending":
            if state["status"] == "pending":
                state["status_at"] = row.created_at
                state["status_event_id"] = row.id
                state["status_build_id"] = row.build_id
        # task_referenced: no-op, matches the global status semantics.

    # Single bulk UPDATE per task. With ~2k tasks this is fast.
    update_stmt = sa.text(
        """
        UPDATE tasks
        SET latest_status = :status,
            latest_status_at = :status_at,
            latest_status_event_id = :status_event_id,
            latest_status_build_id = :status_build_id,
            latest_started_at = :started_at,
            latest_completed_at = :completed_at,
            latest_error_message = :error_message,
            latest_waiting_for_lock = :waiting_for_lock,
            latest_commit_hash = :commit_hash
        WHERE id = :task_id
        """
    )
    for task_id, state in states.items():
        connection.execute(update_stmt, {"task_id": task_id, **state})


def upgrade() -> None:
    """Upgrade schema and backfill latest_* columns from events."""
    # Add columns NULLABLE first so the ALTER doesn't fail on existing rows.
    # latest_status and latest_waiting_for_lock are NOT NULL but get a
    # server_default so the ALTER succeeds on existing rows. After backfill
    # we drop the server_default so future writes must be explicit.
    op.add_column(
        "tasks",
        sa.Column(
            "latest_status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "tasks",
        sa.Column("latest_status_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("latest_status_event_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("latest_status_build_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("latest_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("latest_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("latest_error_message", sa.Text(), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "latest_waiting_for_lock",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column("latest_commit_hash", sa.String(length=64), nullable=True),
    )
    op.create_index(
        op.f("ix_tasks_latest_status"), "tasks", ["latest_status"], unique=False
    )
    op.create_foreign_key(
        "fk_tasks_latest_status_build_id",
        "tasks",
        "builds",
        ["latest_status_build_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Backfill from existing events.
    _backfill_latest_status(op.get_bind())

    # Drop the server_defaults — the application now owns these columns.
    op.alter_column("tasks", "latest_status", server_default=None)
    op.alter_column("tasks", "latest_waiting_for_lock", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("fk_tasks_latest_status_build_id", "tasks", type_="foreignkey")
    op.drop_index(op.f("ix_tasks_latest_status"), table_name="tasks")
    op.drop_column("tasks", "latest_commit_hash")
    op.drop_column("tasks", "latest_waiting_for_lock")
    op.drop_column("tasks", "latest_error_message")
    op.drop_column("tasks", "latest_completed_at")
    op.drop_column("tasks", "latest_started_at")
    op.drop_column("tasks", "latest_status_build_id")
    op.drop_column("tasks", "latest_status_event_id")
    op.drop_column("tasks", "latest_status_at")
    op.drop_column("tasks", "latest_status")
