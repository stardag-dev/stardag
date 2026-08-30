"""add builds.scheduler_lease_until / scheduler_lease_owner

The reactive tick's single-flight lease moves from ``distributed_locks``
(the one live use of a deprecated subsystem) onto the build row it is
about. Both readers — ``is_scheduler_live`` and ``select_wake_candidates``
— then answer from the build they already have, instead of assembling a
lock name from a build id and querying a second table.

Two nullable columns, no index and **no backfill**. A lease is transient
by nature, so a lease held across the migration itself is simply not seen.
The readers filter on environment and RUNNING status first, which
``ix_builds_environment_status`` already narrows to a handful of rows, so
the expiry comparison needs no index of its own.

**The deploy window is longer than the migration, and costs more than one
duplicate tick.** The API deploys first; Modal apps bake their SDK into
the image and are redeployed by hand. Until an app is redeployed, its
old-SDK ticks take the legacy ``distributed_locks`` lease, which this
server no longer reads — so ``notify`` answers ``scheduler_live=False`` for
every task completion and every worker spawns a tick, each of which
immediately no-ops on the legacy lock it *can* still see. That is a
temporary regression to the pre-STA-7 behaviour ("seven tasks, seven cold
starts, no work"), lasting per app until it is redeployed.

Correctness is preserved throughout — an old SDK still single-flights on
the old lock, a new one on the column, and neither can drive a build the
other is driving, because they never share a build. The cost is containers.
If that matters for a deployment, redeploy the apps promptly after the API,
or add a one-release shim that also checks ``distributed_locks`` for
``__scheduler__:{build_id}``.

Revision ID: b41c7d9e2f08
Revises: 97ce4e3cbf32
Create Date: 2026-08-30 19:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b41c7d9e2f08"
down_revision: Union[str, Sequence[str], None] = "97ce4e3cbf32"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "builds",
        sa.Column("scheduler_lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "builds",
        sa.Column("scheduler_lease_owner", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("builds", "scheduler_lease_owner")
    op.drop_column("builds", "scheduler_lease_until")
