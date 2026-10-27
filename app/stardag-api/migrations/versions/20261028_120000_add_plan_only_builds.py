"""add plan-only builds: build.plan_only, REGISTERED, BUILD_REGISTERED

A plan-only build registers its roots and their static closure under its
scope and runs nothing (STA-150). It is created PENDING, never RUNNING, and
ends in the new terminal status ``registered`` with a ``build_registered``
event.

- ``build.plan_only``: NOT NULL, server default false, so every existing
  build reads as an ordinary one. No index: nothing filters on it.
- ``build_status`` gains ``registered``; ``event_type`` gains
  ``build_registered``.
- ``ck_event_build_level_has_no_plan`` is recreated with
  ``build_registered`` among the build-level event types, and now compares
  ``event_type::text``: Postgres refuses to *use* an enum label in the
  transaction that added it ("unsafe use of new value"), and a CHECK
  comparing the enum itself would cast the new label. As text, the whole
  migration's DDL stays in one transaction (the test suite migrates inside
  an outer one), and so will the next label added this way.
- The CHECK is added ``NOT VALID`` and validated in a second step:
  validating scans every ``event`` row, and under the ``ADD CONSTRAINT``'s
  ACCESS EXCLUSIVE lock that scan would block every event append for its
  duration. ``VALIDATE CONSTRAINT`` takes only SHARE UPDATE EXCLUSIVE, which
  writes pass, so it runs in an autocommit block after the rest has
  committed. New rows are checked from the ``ADD`` on either way. Inside a
  caller's transaction (the test suite's guard tests) there is no commit to
  make, and the validation runs inline.

**Rolling deploys and rollback.** Additive for the deploy itself: an old
server never writes the new labels and ignores the column. But an old
server *cannot read* a ``registered`` build or a ``build_registered``
event (its native-enum mapping raises on the unknown label), so:

- during a rolling deploy, an old instance still serving next to a new one
  fails reads of any plan-only build a new instance has already ended —
  a window as long as the rollout, and only for that build's reads;
- **rolling the image back once a ``registered`` build exists requires
  running this downgrade first.** The downgrade rewrites what an old server
  could not read: a REGISTERED build becomes CANCELLED (terminal, ran
  nothing) and a ``build_registered`` event becomes ``build_cancelled``,
  then drops the column and restores the old CHECK. Postgres cannot drop an
  enum label, so the two labels stay in the types, unused.

Revision ID: 5f1b7c3e9a20
Revises: 3c8e1a7d5b42
Create Date: 2026-10-28 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5f1b7c3e9a20"
down_revision: Union[str, Sequence[str], None] = "3c8e1a7d5b42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHECK = "ck_event_build_level_has_no_plan"
_OLD_BUILD_EVENTS = (
    "'build_started', 'build_resumed', 'build_completed', 'build_failed', "
    "'build_cancelled', 'build_exit_early'"
)
_NEW_BUILD_EVENTS = _OLD_BUILD_EVENTS + ", 'build_registered'"


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "ALTER TYPE build_status ADD VALUE IF NOT EXISTS 'registered'"
        " AFTER 'exit_early'"
    )
    op.execute(
        "ALTER TYPE event_type ADD VALUE IF NOT EXISTS 'build_registered'"
        " AFTER 'build_exit_early'"
    )
    op.add_column(
        "build",
        sa.Column("plan_only", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.drop_constraint(_CHECK, "event", type_="check")
    op.execute(
        f"ALTER TABLE event ADD CONSTRAINT {_CHECK} CHECK (plan_id IS NULL OR"
        f" event_type::text NOT IN ({_NEW_BUILD_EVENTS})) NOT VALID"
    )
    _validate(_CHECK)


def _validate(constraint: str) -> None:
    """``VALIDATE CONSTRAINT`` outside the DDL's transaction, so its scan of
    ``event`` holds SHARE UPDATE EXCLUSIVE (writes pass) rather than the
    ``ADD CONSTRAINT``'s ACCESS EXCLUSIVE. Inline when the caller owns the
    transaction: there is nothing this migration may commit."""
    statement = f"ALTER TABLE event VALIDATE CONSTRAINT {constraint}"
    context = op.get_context()
    if getattr(context, "_in_external_transaction", False):
        op.execute(statement)
        return
    with context.autocommit_block():
        op.execute(statement)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("UPDATE build SET status = 'cancelled' WHERE status = 'registered'")
    op.execute(
        "UPDATE event SET event_type = 'build_cancelled'"
        " WHERE event_type = 'build_registered'"
    )
    op.drop_constraint(_CHECK, "event", type_="check")
    op.execute(
        f"ALTER TABLE event ADD CONSTRAINT {_CHECK} CHECK (plan_id IS NULL OR"
        f" event_type NOT IN ({_OLD_BUILD_EVENTS})) NOT VALID"
    )
    op.drop_column("build", "plan_only")
    _validate(_CHECK)
