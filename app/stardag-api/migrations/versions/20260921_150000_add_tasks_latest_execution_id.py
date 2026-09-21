"""add tasks.latest_execution_id

Gives an execution an identity: a UUID minted by the client that is about
to run the task, carried on the claiming start, on the tick's
ref-recording start, on the worker's own self-report, and on that worker's
interruption and preemption reports. See
``docs/design/executions-as-records.md``.

The claim is taken *before* the spawn — the execution claim and any
concurrency-limit slots have to be acquired in one transaction, so a
denied task never occupies a worker — which means there is no executor ref
at claim time. Without one, two pairs of requests were indistinguishable:

1. A retried claiming start (the registry client re-sending a POST whose
   response was lost) versus a genuine second attempt of the same build.
   Both were refused, so a worker stood down from a task it held the claim
   on and the task sat claimed and not running until the claim expired.
2. A worker's self-report for the execution the task still holds versus
   one from an execution whose claim lapsed and was taken over meanwhile.
   Both were applied, so a restarted straggler could evict the live holder
   and produce the second execution claims exist to prevent.

One nullable column, no index, **no backfill**.

No index because every reader already has the task row in hand — the
comparison happens on the row the transition has locked FOR UPDATE, never
as a lookup key.

No backfill because there is nothing to backfill *from*. The identity is
minted by the client, so no execution that started before this column
existed has one, and inventing ids here would mint identities for
executions that cannot know them — the only readers are requests carrying
an id to compare against, and a fabricated value could only produce a
false mismatch. NULL is the honest answer and is exactly the
pre-identity behaviour.

Additive in both directions of a rolling deploy, which is what lets this
ship before the SDK:

- An SDK predating the column sends no id. Nothing is written, nothing is
  compared, and both rules fall back to what they did before — the
  ``(executor, executor_ref)`` pair for the claim retry, and build
  ownership plus the ref for report validity.
- A new SDK against a server predating the column has its id ignored. It
  loses the two protections above, which is again today's behaviour; no
  ``minimum_version`` bump, because nothing degrades below it.
- A task RUNNING across the deploy keeps a NULL id, and the refusal rule
  is written to treat NULL as "no opinion" rather than as a mismatch, so
  no in-flight worker's report is refused by the upgrade itself.

Downgrade drops the column. Nothing else reads it, and the identities are
not reconstructible, so a re-upgrade starts from NULL again — harmless
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
