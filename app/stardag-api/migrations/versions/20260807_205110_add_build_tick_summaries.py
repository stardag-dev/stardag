"""add build tick summaries

Revision ID: 0807aacc230e
Revises: 792fb8ab3c3c
Create Date: 2026-08-07 20:51:10.251057

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0807aacc230e"
down_revision: Union[str, Sequence[str], None] = "792fb8ab3c3c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Per-build trail of reactive scheduler tick outcomes. ``outcome`` is
    # promoted out of the payload (small closed-ish vocabulary, worth
    # filtering on); ``summary`` keeps the SDK-owned dict verbatim so new
    # fields need no migration. New table, no backfill.
    op.create_table(
        "build_tick_summaries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("build_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column(
            "summary",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["build_id"], ["builds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # The only query is "newest N for this build" (read endpoint and the
    # insert-time retention prune); also serves plain build_id lookups and
    # the FK cascade, so there is no separate index on build_id.
    op.create_index(
        "ix_build_tick_summaries_build_created",
        "build_tick_summaries",
        ["build_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_build_tick_summaries_build_created", table_name="build_tick_summaries"
    )
    op.drop_table("build_tick_summaries")
