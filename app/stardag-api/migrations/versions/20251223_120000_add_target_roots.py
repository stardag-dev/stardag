"""add target_roots table

Revision ID: 002_target_roots
Revises: 8a2a0c6c2c26
Create Date: 2025-12-23 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002_target_roots"
down_revision: Union[str, Sequence[str], None] = "8a2a0c6c2c26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "target_roots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("uri_prefix", sa.String(512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "workspace_id", "name", name="uq_target_root_workspace_name"
        ),
    )
    op.create_index(
        "ix_target_roots_workspace_id",
        "target_roots",
        ["workspace_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_target_roots_workspace_id", table_name="target_roots")
    op.drop_table("target_roots")
