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

Three indexes serve the ``GET /tasks`` filters and order: a multicolumn
trigram GIN index over ``task_name``, ``task_namespace`` and
``description`` (``q``, an ``ILIKE '%x%'`` per column; needs ``pg_trgm``,
created here if missing — a trusted extension since Postgres 13, so the
database owner may create it), a ``text_pattern_ops`` btree on
``(environment_id, task_id)`` (``id_prefix``: under a non-C collation the
unique index cannot serve ``LIKE 'abc%'``), and ``(environment_id,
status_at, id)`` for the listing's order and keyset. Plain ``CREATE
INDEX``: it blocks writes to ``task`` while it builds, a few seconds per
half a million rows.

Additive in both directions of a rolling deploy: an old server never reads
or writes either column, a new one reads the defaults on old rows.
Downgrade drops the indexes and both columns, and the annotations with
them. It leaves ``pg_trgm`` installed: ``IF NOT EXISTS`` cannot record
whether this revision created it, and something else may use it.

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
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # The operator class qualified with the schema the extension lives in:
    # if it was already installed, that need not be on ``search_path``.
    trgm_schema = op.get_bind().scalar(
        sa.text(
            "SELECT n.nspname FROM pg_extension e"
            " JOIN pg_namespace n ON n.oid = e.extnamespace"
            " WHERE e.extname = 'pg_trgm'"
        )
    )
    trgm_ops = f'"{trgm_schema}".gin_trgm_ops'
    op.create_index(
        "ix_task_search_trgm",
        "task",
        ["task_name", "task_namespace", "description"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={
            "task_name": trgm_ops,
            "task_namespace": trgm_ops,
            "description": trgm_ops,
        },
    )
    op.create_index(
        "ix_task_environment_task_id_prefix",
        "task",
        ["environment_id", "task_id"],
        unique=False,
        postgresql_ops={"task_id": "text_pattern_ops"},
    )
    op.create_index(
        "ix_task_environment_status_at",
        "task",
        ["environment_id", "status_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema. ``pg_trgm`` stays installed (module docstring)."""
    op.drop_index("ix_task_environment_status_at", table_name="task")
    op.drop_index("ix_task_environment_task_id_prefix", table_name="task")
    op.drop_index("ix_task_search_trgm", table_name="task", postgresql_using="gin")
    op.drop_index("ix_task_tags", table_name="task", postgresql_using="gin")
    op.drop_column("task", "description")
    op.drop_column("task", "tags")
