"""add tasks.static_deps_declared

A task's declared static upstream set is immutable: once the registry has
recorded one, a later registration declaring a different set is refused
(see ``docs/design/immutable-dependency-declarations.md``). The recorded
set lives in ``task_dependencies`` — except when it is **empty**, which
writes no rows at all, so "this task was declared to require nothing" and
"nobody has ever declared anything about this task" are the same absence.

Without a flag the check therefore does not apply to the one shape it most
needs to: a leaf whose ``requires()`` later returns an upstream. That reads
as a first declaration and is accepted, and the task's promise changes
without its id moving.

One non-nullable boolean defaulting to false, and **no backfill**. False
for every existing row means the next declaration for a task registered
before this migration is treated as its first, which is the same
permissiveness the environment already had — the flag only ever makes the
check stricter, and only from the next registration onwards. Backfilling
"true wherever edges exist" would be wrong in the other direction: it would
start refusing declarations for tasks whose recorded edges are the
accumulated union of several historical sets, which is exactly the
inconsistency the rule exists to surface rather than to enforce
retroactively.

No index: the flag is only ever read alongside the task row the check has
already loaded.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7c2e1a4b8d63"
down_revision: Union[str, Sequence[str], None] = "b41c7d9e2f08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "static_deps_declared",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("tasks", "static_deps_declared")
