"""add task.tags and task.description

Annotations of a completion (STA-149): free-form tags for discovery and a
description, set by ``PATCH /tasks/{task_id}`` and filtered on by ``GET
/tasks?tag=&q=``. Mutable, and outside every hash and every registration
payload: registration inserts ``task`` rows with ``ON CONFLICT DO NOTHING``
and never names these columns, so it can neither set nor reset them.

``tags`` is ``text[] NOT NULL DEFAULT '{}'``: a constant default, so adding
it to existing rows is a catalog-only change (Postgres 11+), and every
existing task reads as untagged. A GIN index serves the containment
filter (``tags @> ARRAY[...]``). ``description`` is a nullable ``text``.

Additive in both directions of a rolling deploy: an old server never reads
or writes either column, a new one reads the defaults on old rows.
Downgrade drops both, and the annotations with them.

Revision ID: 7584698b441f
Revises: 3c8e1a7d5b42
Create Date: 2026-10-27 16:04:31.646196

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "7584698b441f"
down_revision: Union[str, Sequence[str], None] = "3c8e1a7d5b42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "task",
        sa.Column(
            "tags", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False
        ),
    )
    op.add_column("task", sa.Column("description", sa.Text(), nullable=True))
    op.create_index(
        "ix_task_tags", "task", ["tags"], unique=False, postgresql_using="gin"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_task_tags", table_name="task", postgresql_using="gin")
    op.drop_column("task", "description")
    op.drop_column("task", "tags")
