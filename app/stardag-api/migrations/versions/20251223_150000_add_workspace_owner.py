"""Add owner_id to workspaces for personal workspaces.

Revision ID: 20251223_150000
Revises: 20251223_120000
Create Date: 2025-12-23 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003_workspace_owner"
down_revision: str | None = "002_target_roots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add owner_id column (nullable - only personal workspaces have an owner)
    op.add_column(
        "workspaces",
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_index(
        "ix_workspaces_owner_id",
        "workspaces",
        ["owner_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_workspaces_owner_id", table_name="workspaces")
    op.drop_column("workspaces", "owner_id")
