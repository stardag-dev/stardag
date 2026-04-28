"""indices on env_id+created_at and JSONB column types

Revision ID: 7b8476ac0846
Revises: 94003640952d
Create Date: 2026-04-27 21:23:42.024329

Two related changes that improve Task Explorer / search performance on
production-scale data:

1. Convert the four "json" columns to "jsonb": tasks.task_data,
   events.event_metadata, task_artifacts.body_json, builds.root_task_ids.
   The application has always used these as structured data; "json" stores
   them as text and reparses on every -> / ->> access (used by the search
   route). "jsonb" parses once on write, keeps a binary representation, and
   unblocks GIN indexing if we want it later.

2. Add btree(environment_id, created_at) to tasks and task_artifacts. The
   Task Explorer base case ("show me my recent tasks", "list distinct keys
   in artifacts") sorts by created_at, which previously triggered a sort
   step on top of the env-filtered scan.

The ALTER COLUMN ... TYPE jsonb USING ...::jsonb operation rewrites each
table once. On the production data sizes observed during the 2026-04-27
incident (events 1672 kB heap, tasks 2272 kB) this completes in seconds.
The downgrade path goes the other way (jsonb -> json) which preserves the
data and is also a one-pass rewrite.

LOCK / DOWNTIME NOTE: ALTER COLUMN takes ACCESS EXCLUSIVE on the table
for the duration of the rewrite, and op.create_index (without
CONCURRENTLY) takes a SHARE lock that blocks concurrent writes. Both
are sized for the current shape — a few seconds of write-blocking is
acceptable. If the data ever grows by an order of magnitude, revisit
this migration shape: CREATE INDEX CONCURRENTLY (which means stepping
out of transactional DDL), or a column-swap pattern (add new jsonb
column → backfill → DROP old → RENAME) for the type changes.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "7b8476ac0846"
down_revision: Union[str, Sequence[str], None] = "94003640952d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Column type alias for readability — JSON in the model declaration with the
# postgresql JSONB variant; alembic resolves this to jsonb on Postgres.
_jsonb_variant = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)
_existing_json = postgresql.JSON(astext_type=sa.Text())


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "builds",
        "root_task_ids",
        existing_type=_existing_json,
        type_=_jsonb_variant,
        existing_nullable=False,
        postgresql_using="root_task_ids::jsonb",
    )
    op.alter_column(
        "events",
        "event_metadata",
        existing_type=_existing_json,
        type_=_jsonb_variant,
        existing_nullable=True,
        postgresql_using="event_metadata::jsonb",
    )
    op.alter_column(
        "task_artifacts",
        "body_json",
        existing_type=_existing_json,
        type_=_jsonb_variant,
        existing_nullable=False,
        postgresql_using="body_json::jsonb",
    )
    op.create_index(
        "ix_task_artifacts_environment_created",
        "task_artifacts",
        ["environment_id", "created_at"],
        unique=False,
    )
    op.alter_column(
        "tasks",
        "task_data",
        existing_type=_existing_json,
        type_=_jsonb_variant,
        existing_nullable=False,
        postgresql_using="task_data::jsonb",
    )
    op.create_index(
        "ix_tasks_environment_created",
        "tasks",
        ["environment_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_tasks_environment_created", table_name="tasks")
    op.alter_column(
        "tasks",
        "task_data",
        existing_type=_jsonb_variant,
        type_=_existing_json,
        existing_nullable=False,
        postgresql_using="task_data::json",
    )
    op.drop_index("ix_task_artifacts_environment_created", table_name="task_artifacts")
    op.alter_column(
        "task_artifacts",
        "body_json",
        existing_type=_jsonb_variant,
        type_=_existing_json,
        existing_nullable=False,
        postgresql_using="body_json::json",
    )
    op.alter_column(
        "events",
        "event_metadata",
        existing_type=_jsonb_variant,
        type_=_existing_json,
        existing_nullable=True,
        postgresql_using="event_metadata::json",
    )
    op.alter_column(
        "builds",
        "root_task_ids",
        existing_type=_jsonb_variant,
        type_=_existing_json,
        existing_nullable=False,
        postgresql_using="root_task_ids::json",
    )
