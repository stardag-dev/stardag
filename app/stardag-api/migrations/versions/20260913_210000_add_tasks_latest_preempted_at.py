"""add tasks.latest_preempted_at

Records when the execution backend last said it was restarting a task's
execution itself — a preemption (``TASK_PREEMPTED``). A preempted worker
used to report nothing at all, on the reasoning that the restart makes the
report unnecessary. That holds right up until the restart does not come,
at which point the task reads as "running happily since T0" and stays that
way until its whole claim lapses — which, sized from a worker function's
timeout, can be a day.

One nullable column, no index, **no backfill**. A preemption is a
transient fact about an execution in flight, so one that happened before
this ran is simply not recorded; the rows that matter are written from
here on. Readers reach it through the task's own row, or after narrowing
on ``latest_status`` via ``ix_tasks_environment_status``, so a
handful of RUNNING rows is all the comparison ever sees.

**Never cleared, deliberately.** "Is a restart still outstanding?" is
derived rather than stored — ``latest_status == RUNNING and
latest_preempted_at > latest_status_at`` — so the restarted execution's
own ``TASK_STARTED`` falsifies it by moving ``latest_status_at``. One
write site, no clear sites, and no way for the two to disagree.

Additive for every client: the column defaults to NULL, which reads as
"no preemption recorded", and an SDK that never posts ``/preempt`` behaves
exactly as before.

Revision ID: c5f2a8b71d34
Revises: b41c7d9e2f08
Create Date: 2026-09-13 21:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c5f2a8b71d34"
down_revision: Union[str, Sequence[str], None] = "b41c7d9e2f08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tasks",
        sa.Column("latest_preempted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("tasks", "latest_preempted_at")
