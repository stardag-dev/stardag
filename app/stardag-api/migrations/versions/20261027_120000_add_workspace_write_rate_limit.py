"""add workspaces.max_write_requests_per_minute

The hosting operator's per-workspace override of the v2 write rate limit
(``LIMITS_MAX_REQUESTS_PER_MINUTE``), STA-135. A cost control of whoever
runs the registry, not a workspace setting: no API route reads or writes
it, and it is set directly in the database. NULL means "the deployment's
default", 0 "refuse the workspace's new work"; a CHECK keeps it >= 0.

One nullable column, no index, no backfill. Additive in both directions of
a rolling deploy: an old server ignores it, a new one reading NULL applies
the default as before. Downgrade drops it.

Revision ID: 3c8e1a7d5b42
Revises: 7b2d9e4c1f30
Create Date: 2026-10-27 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c8e1a7d5b42"
down_revision: Union[str, Sequence[str], None] = "7b2d9e4c1f30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "workspaces",
        sa.Column("max_write_requests_per_minute", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_workspaces_write_rate_limit_not_negative",
        "workspaces",
        "max_write_requests_per_minute IS NULL OR max_write_requests_per_minute >= 0",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "ck_workspaces_write_rate_limit_not_negative", "workspaces", type_="check"
    )
    op.drop_column("workspaces", "max_write_requests_per_minute")
