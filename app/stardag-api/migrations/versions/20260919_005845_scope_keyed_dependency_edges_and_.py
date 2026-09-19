"""scope-keyed dependency edges and deployments

Dependency edges become facts about the code and structure config that
evaluated them rather than about the task id: every edge and every build
carries a ``scope_key``, and a build's readiness is evaluated over the edges
in its own scope only. The ``deployments`` table records which code versions
of an app have been deployed (the newest is current). Phantom placeholder
rows stop existing.
See ``docs/design/scope-keyed-dependency-structure.md``.

Schema:

- ``builds.scope_key`` (non-null) and ``builds.build_config``.
- ``events.scope_key`` (nullable) on registration events: the scope a task
  was registered into the build under, which is what plan membership reads.
- ``task_dependencies.scope_key`` (nullable — NULL marks a row written before
  scopes existed; such rows gate nothing and count everywhere in the graph
  view). The unique edge becomes ``(scope_key, upstream, downstream)``, with
  ``(scope_key, downstream)`` indexed for the gating probe.
- ``deployments``.

Data, in this order:

1. **Phantom rows are deleted.** A phantom was a placeholder for a task an
   edge named but nobody registered; the concept is gone (an unknown upstream
   is now a 400), and the rows are the crash-orphan case with nothing worth
   keeping. Their edges, events and limit keys go with them by cascade.
2. **Every build gets the synthetic scope** ``build:<id>``, which nothing
   else shares. That is exactly the scope a build that never sets a real one
   runs under after this migration, so the two populations behave alike.
3. **Legacy edges are copied into the scope of every RUNNING build that
   holds their downstream task.** Without this a reactive build in flight
   across the deploy would lose every gate at once — its next frontier read
   would find no edges in its scope — and run downstream tasks early. The
   copy over-approximates (a running build inherits every edge ever
   recorded for its tasks, which is what it had before) and is bounded by
   the number of running builds. Terminal builds' edges stay NULL as
   history.

The deploy window: an SDK predating scopes never sets one and keeps working
on the synthetic scope; a new SDK against this server sets a real one. No
``minimum_version`` bump.

Downgrade drops the columns and the table. It has to remove the scoped
copies made in step 3 first, because the old unique constraint on
``(upstream, downstream)`` cannot hold with them present — so a downgrade
discards every edge written under a scope, which is every edge registered
after this migration. Phantom rows are not restored.

Revision ID: 690e61e0c920
Revises: c5f2a8b71d34
Create Date: 2026-09-19 00:58:45.788418

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "690e61e0c920"
down_revision: Union[str, Sequence[str], None] = "c5f2a8b71d34"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "deployments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("app_name", sa.String(length=64), nullable=False),
        sa.Column("code_id", sa.String(length=64), nullable=False),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("modal_app_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "environment_id", "app_name", "code_id", name="uq_deployment_app_code"
        ),
    )
    op.create_index(
        op.f("ix_deployments_code_id"), "deployments", ["code_id"], unique=False
    )
    op.create_index(
        "ix_deployments_environment_app_deployed",
        "deployments",
        ["environment_id", "app_name", "deployed_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deployments_environment_id"),
        "deployments",
        ["environment_id"],
        unique=False,
    )

    # 1. Phantoms go. Cascades take their edges, events and limit keys.
    op.execute("DELETE FROM tasks WHERE is_phantom")

    # 2. Builds: the column arrives nullable, is backfilled with the
    # synthetic per-build scope, and is then made non-null.
    op.add_column("builds", sa.Column("scope_key", sa.String(length=96), nullable=True))
    op.execute("UPDATE builds SET scope_key = 'build:' || CAST(id AS TEXT)")
    op.alter_column("builds", "scope_key", nullable=False)
    op.add_column(
        "builds",
        sa.Column(
            "build_config",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
    )

    # 2b. Registration events carry the scope they were made under: a
    # build's plan under its current scope is exactly those events. Every
    # pre-migration registration was made under the build's (synthetic)
    # scope, which is what the backfill says.
    op.add_column("events", sa.Column("scope_key", sa.String(length=96), nullable=True))
    op.create_index("ix_events_build_scope", "events", ["build_id", "scope_key"])
    op.execute(
        """
        UPDATE events SET scope_key = b.scope_key
        FROM builds b
        WHERE events.build_id = b.id
          AND events.event_type IN ('task_pending', 'task_referenced')
        """
    )

    # 3. Edges: the column, the constraint swap, then the copies for builds
    # in flight. The old unique constraint has to go before the copies land,
    # since a copy duplicates a legacy row on (upstream, downstream).
    op.add_column(
        "task_dependencies",
        sa.Column("scope_key", sa.String(length=96), nullable=True),
    )
    op.drop_constraint(
        op.f("uq_task_dependency_edge"), "task_dependencies", type_="unique"
    )
    op.create_index(
        "ix_task_dep_scope_downstream",
        "task_dependencies",
        ["scope_key", "downstream_task_id"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_task_dependency_scope_edge",
        "task_dependencies",
        ["scope_key", "upstream_task_id", "downstream_task_id"],
    )
    op.execute(
        """
        INSERT INTO task_dependencies
            (id, upstream_task_id, downstream_task_id, scope_key, is_dynamic,
             created_at)
        SELECT
            gen_random_uuid(),
            copies.upstream_task_id,
            copies.downstream_task_id,
            copies.scope_key,
            copies.is_dynamic,
            copies.created_at
        FROM (
            -- One row per (edge, build): a running downstream normally has
            -- several events in its build, and a DISTINCT over a generated
            -- uuid would keep every one of them, then trip the new unique
            -- constraint on (scope_key, upstream, downstream).
            SELECT DISTINCT
                e.upstream_task_id,
                e.downstream_task_id,
                b.scope_key,
                e.is_dynamic,
                e.created_at
            FROM task_dependencies e
            JOIN events ev ON ev.task_id = e.downstream_task_id
            JOIN builds b ON b.id = ev.build_id
            WHERE e.scope_key IS NULL
              AND b.latest_status = 'running'
        ) AS copies
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "uq_task_dependency_scope_edge", "task_dependencies", type_="unique"
    )
    op.drop_index("ix_task_dep_scope_downstream", table_name="task_dependencies")
    # Scoped rows cannot coexist with the old (upstream, downstream) unique
    # constraint; see the module docstring for what this discards.
    op.execute("DELETE FROM task_dependencies WHERE scope_key IS NOT NULL")
    op.create_unique_constraint(
        op.f("uq_task_dependency_edge"),
        "task_dependencies",
        ["upstream_task_id", "downstream_task_id"],
        postgresql_nulls_not_distinct=False,
    )
    op.drop_column("task_dependencies", "scope_key")
    op.drop_index("ix_events_build_scope", table_name="events")
    op.drop_column("events", "scope_key")
    op.drop_column("builds", "build_config")
    op.drop_column("builds", "scope_key")
    op.drop_index(op.f("ix_deployments_environment_id"), table_name="deployments")
    op.drop_index("ix_deployments_environment_app_deployed", table_name="deployments")
    op.drop_index(op.f("ix_deployments_code_id"), table_name="deployments")
    op.drop_table("deployments")
