"""add build.scheduler_lease_released_at

When a tick last released the build's scheduler lease. ``wake-candidates``
treats a hand-out (``build_wake.tick_requested_at``) older than it as spent:
the tick that hand-out spawned has run and ended, so a flag landing after it
is handed out at once instead of after the 120 s hand-out window (STA-34).

One nullable column, no index, **no backfill**. The only reader is the
wake-candidates query, which already joins ``build`` by primary key; NULL
means "never released here", which keeps a hand-out's full window -- exactly
the behaviour before this column existed.

Additive in both directions of a rolling deploy: an old server neither
writes nor reads it, and a new one reading NULL behaves as the old one did.
Downgrade drops it; nothing else reads it.

Revision ID: 7b2d9e4c1f30
Revises: 630d475de408
Create Date: 2026-09-26 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b2d9e4c1f30"
down_revision: Union[str, Sequence[str], None] = "630d475de408"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "build",
        sa.Column(
            "scheduler_lease_released_at", sa.DateTime(timezone=True), nullable=True
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("build", "scheduler_lease_released_at")
