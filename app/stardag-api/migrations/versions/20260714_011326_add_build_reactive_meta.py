"""add build reactive_meta

Revision ID: d31ca54ec8ab
Revises: 966bad447840
Create Date: 2026-07-14 01:13:26.947962

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d31ca54ec8ab"
down_revision: Union[str, Sequence[str], None] = "966bad447840"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Reactive-scheduling metadata. ``reactive_app_name`` is the owner +
    # marker (presence => reactively scheduled), a typed/indexed column so
    # "RUNNING reactive builds owned by app X" is a server-side filter;
    # ``reactive_tick_kwargs`` holds the SDK-owned TickConfig kwargs (JSONB).
    # Both nullable/additive — an instant, no-backfill migration.
    op.add_column(
        "builds",
        sa.Column("reactive_app_name", sa.String(length=64), nullable=True),
    )
    op.create_index(
        op.f("ix_builds_reactive_app_name"),
        "builds",
        ["reactive_app_name"],
        unique=False,
    )
    op.add_column(
        "builds",
        sa.Column(
            "reactive_tick_kwargs",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("builds", "reactive_tick_kwargs")
    op.drop_index(op.f("ix_builds_reactive_app_name"), table_name="builds")
    op.drop_column("builds", "reactive_app_name")
