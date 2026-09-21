"""add tasks.latest_execution_id

Gives a claim an identity: a UUID minted by the caller before it claims
a task, so a claiming start can say *which* attempt it is. See
``docs/design/executions-as-records.md``.

The claim is taken **before** the spawn -- the claim and any
concurrency-limit slots have to be acquired in one transaction, so a
denied task never occupies a worker -- which means there is no executor
ref at claim time and never was. Without one, two requests were
indistinguishable: a retried claiming start (the registry client
re-sending a POST whose response was lost) and a genuine second attempt
of the same build. Both were refused, so a worker stood down from a task
it held the claim on, and the task then sat claimed and not running
until the claim expired.

One nullable column, no index, **no backfill**.

No index because the only reader already has the task row in hand -- the
comparison happens on the row the claim transaction has locked FOR
UPDATE, never as a lookup key.

No backfill because there is nothing to backfill *from*. The identity is
minted by the caller, so no claim taken before this column existed has
one, and inventing values here would mint identities for claims that
cannot know them -- the only reader is a request carrying an id to
compare against, and a fabricated value could only produce a false
mismatch. NULL is the honest answer, and it is exactly the pre-identity
behaviour.

Additive in both directions of a rolling deploy, which is what lets this
ship before the SDK:

- An SDK predating the column sends no id, so nothing is written and
  nothing compared: the ``(executor, executor_ref)`` pair decides, as
  before.
- A new SDK against a server predating the column has its id ignored and
  loses only the idempotent retry, which is again today's behaviour. No
  ``minimum_version`` bump, because nothing degrades below it.

Downgrade drops the column. Nothing else reads it and the identities are
not reconstructible, so a re-upgrade starts from NULL again -- harmless,
for the same reason the absent backfill is.

Revision ID: a3c1f0d47b28
Revises: 690e61e0c920
Create Date: 2026-09-21 15:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a3c1f0d47b28"
down_revision: Union[str, Sequence[str], None] = "690e61e0c920"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tasks",
        sa.Column("latest_execution_id", sa.Uuid(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("tasks", "latest_execution_id")
