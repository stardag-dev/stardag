"""Rename task_family to task_name.

Revision ID: 20260104_120000
Revises: 003_workspace_owner
Create Date: 2026-01-04

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260104_120000"
down_revision: Union[str, None] = "003_workspace_owner"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop old index
    op.drop_index("ix_tasks_workspace_family", table_name="tasks")

    # Rename column
    op.alter_column("tasks", "task_family", new_column_name="task_name")

    # Create new index
    op.create_index("ix_tasks_workspace_name", "tasks", ["workspace_id", "task_name"])


def downgrade() -> None:
    # Drop new index
    op.drop_index("ix_tasks_workspace_name", table_name="tasks")

    # Rename column back
    op.alter_column("tasks", "task_name", new_column_name="task_family")

    # Recreate old index
    op.create_index(
        "ix_tasks_workspace_family", "tasks", ["workspace_id", "task_family"]
    )
