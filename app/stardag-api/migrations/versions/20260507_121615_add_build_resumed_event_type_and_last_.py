"""add build_resumed event type and last_active_at column

Revision ID: c1ea60e468b8
Revises: 226b6ba1fa16
Create Date: 2026-05-07 12:16:15.511870

The ``BUILD_RESUMED`` enum value lives only in Python (events.event_type
is a free-form ``String(32)``) so this migration only touches the
``builds`` schema:
  * adds ``last_active_at`` (nullable=False) and backfills from
    ``created_at`` so existing rows have a sensible ordering value
  * adds an index on (environment_id, last_active_at) for the new
    list-builds sort order

The column is added nullable, backfilled, and then altered to NOT NULL
in a single migration — this is safe because the table is small in
practice (per-environment build counts) and we hold a transaction anyway.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1ea60e468b8"
down_revision: Union[str, Sequence[str], None] = "226b6ba1fa16"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "builds",
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE builds SET last_active_at = created_at")
    op.alter_column("builds", "last_active_at", nullable=False)
    op.create_index(
        "ix_builds_environment_last_active",
        "builds",
        ["environment_id", "last_active_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_builds_environment_last_active", table_name="builds")
    op.drop_column("builds", "last_active_at")
