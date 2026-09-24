"""registry v2: core schema (task, instance, plan, execution, ...)

Replaces the v1 core tables with the v2 entities of
``docs/design/registry-v2/design.md`` ("Entities"). Group (b) tables
(users, workspaces, environments, members, invites, API keys, target
roots) are untouched.

**No data migration.** v2 is a fully breaking release line: a registry
upgraded to this revision starts empty. Every v1 core table is dropped —
``tasks``, ``task_dependencies``, ``events``, ``deployments``, ``builds``,
``build_tick_summaries``, ``task_limit_keys``, ``task_artifacts``,
``environment_concurrency_limits`` and ``distributed_locks`` (retired: the
claim is the only mutual exclusion, D11) — and the v2 tables are created
empty. The v1 rows cannot be carried over: a v1 ``tasks`` row holds one
first-write-wins parameter body per completion and no scope, so there is
nothing to derive an instance, a plan or a membership from.

**Amended in place, never deployed.** No registry has ever run this
revision (the v2 line is unreleased), so later I0 steps change it here
rather than stacking revisions on a schema nobody has: step 3c moved the
wake-up flags off ``build`` onto ``build_wake`` and added the attempt-count
and quota indexes; I5 added ``build.error_message`` and the
``lost`` execution outcome.

Mechanics worth knowing:

- Every FK between environment-scoped tables is composite and leads with
  ``environment_id`` (the design's environment rule), onto a unique key
  that also leads with it.
- The two pointer FKs on ``task`` (``claim_plan_id`` and ``execution_id``)
  are circular with ``plan_member`` and ``execution``, so they are added
  after both exist, and they use PostgreSQL 15's column-list form
  ``ON DELETE SET NULL (col)``: a plain SET NULL on a composite FK would
  null ``environment_id`` and ``id`` too. The same form is used for the
  nullable pointers on ``event``.
- Those five pointer FKs are ``DEFERRABLE INITIALLY DEFERRED``: deleting a
  build reaches one event (or task) row along several of them in one
  cascade, and an immediate check of the second would fail on a row the
  cascade has already deleted but whose SET NULL has not yet run.

Revision ID: 630d475de408
Revises: a3c1f0d47b28
Create Date: 2026-09-24 00:49:16.196042

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "630d475de408"
down_revision: Union[str, Sequence[str], None] = "a3c1f0d47b28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# v1 core tables, dropped in an order that respects their foreign keys.
# DROP TABLE takes each table's indexes with it.
V1_CORE_TABLES = (
    "events",
    "task_dependencies",
    "task_artifacts",
    "task_limit_keys",
    "build_tick_summaries",
    "distributed_locks",
    "tasks",
    "builds",
    "deployments",
    "environment_concurrency_limits",
)


def upgrade() -> None:
    """Drop the v1 core tables and create the v2 ones, empty."""
    for table in V1_CORE_TABLES:
        op.drop_table(table)

    op.create_table(
        "build",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "root_task_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "executor_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("scheduler_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduler_lease_owner", sa.String(length=64), nullable=True),
        sa.Column("reactive_app_name", sa.String(length=64), nullable=True),
        sa.Column(
            "reactive_tick_kwargs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "completed",
                "failed",
                "cancelled",
                "exit_early",
                name="build_status",
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_triggered_by_user_id", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "is_resumed", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("environment_id", "id", name="uq_build_environment_id"),
    )
    op.create_index(
        "ix_build_environment_created",
        "build",
        ["environment_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_build_environment_last_active",
        "build",
        ["environment_id", "last_active_at"],
        unique=False,
    )
    op.create_index(
        "ix_build_environment_status",
        "build",
        ["environment_id", "status", "last_active_at"],
        unique=False,
    )
    op.create_index(op.f("ix_build_name"), "build", ["name"], unique=False)
    op.create_index(
        op.f("ix_build_reactive_app_name"), "build", ["reactive_app_name"], unique=False
    )
    op.create_index(op.f("ix_build_user_id"), "build", ["user_id"], unique=False)
    op.create_table(
        "build_wake",
        sa.Column("build_id", sa.Uuid(), nullable=False),
        sa.Column("needs_tick_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tick_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "build_id"],
            ["build.environment_id", "build.id"],
            name="fk_build_wake_build",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("build_id"),
    )
    op.create_index(
        "ix_build_wake_flagged",
        "build_wake",
        ["environment_id", "needs_tick_at"],
        unique=False,
        postgresql_where=sa.text("needs_tick_at IS NOT NULL"),
    )
    op.create_table(
        "deployment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind", sa.Enum("modal", "local", name="deployment_kind"), nullable=False
        ),
        sa.Column("app_name", sa.String(length=64), nullable=False),
        sa.Column("code_id", sa.String(length=64), nullable=False),
        sa.Column("image_id", sa.String(length=128), nullable=True),
        sa.Column("modal_app_id", sa.String(length=64), nullable=True),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "environment_id", "id", name="uq_deployment_environment_id"
        ),
        sa.UniqueConstraint(
            "environment_id",
            "kind",
            "app_name",
            "generation",
            name="uq_deployment_app_generation",
        ),
    )
    op.create_index(
        "ix_deployment_app_generation_desc",
        "deployment",
        ["environment_id", "kind", "app_name", sa.literal_column("generation DESC")],
        unique=False,
    )
    op.create_index(
        "uq_deployment_local_code_id",
        "deployment",
        ["environment_id", "code_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'local'"),
    )
    op.create_table(
        "environment_concurrency_limit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("max_concurrent", sa.Integer(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "environment_id", "key", name="uq_environment_concurrency_limit_key"
        ),
    )
    op.create_table(
        "settings",
        sa.Column("hash", sa.Uuid(), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("environment_id", "hash", name="pk_settings"),
    )
    op.create_table(
        "task",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column(
            "task_namespace", sa.String(length=255), server_default="", nullable=False
        ),
        sa.Column("task_name", sa.String(length=255), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("output_uri", sa.String(length=2048), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "completed",
                "failed",
                "cancelled",
                "skipped",
                "suspended",
                "interrupted",
                name="task_status",
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("status_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_plan_id", sa.Uuid(), nullable=True),
        sa.Column("execution_id", sa.Uuid(), nullable=True),
        sa.Column("preempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status <> 'running' OR claim_expires_at IS NOT NULL",
            name="ck_task_running_has_claim_expiry",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("environment_id", "id", name="uq_task_environment_id"),
        sa.UniqueConstraint(
            "environment_id", "task_id", name="uq_task_environment_task_id"
        ),
    )
    op.create_index(
        "ix_task_environment_name",
        "task",
        ["environment_id", "task_name"],
        unique=False,
    )
    op.create_index(
        "ix_task_environment_status",
        "task",
        ["environment_id", "status", "status_at"],
        unique=False,
    )
    op.create_index(
        "ix_task_running_claim_plan",
        "task",
        ["claim_plan_id"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_table(
        "build_tick_summary",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("build_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "build_id"],
            ["build.environment_id", "build.id"],
            name="fk_build_tick_summary_build",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_build_tick_summary_build_created",
        "build_tick_summary",
        ["build_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "plan",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("build_id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("settings_hash", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "build_id"],
            ["build.environment_id", "build.id"],
            name="fk_plan_build",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "deployment_id"],
            ["deployment.environment_id", "deployment.id"],
            name="fk_plan_deployment",
            ondelete="NO ACTION",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "settings_hash"],
            ["settings.environment_id", "settings.hash"],
            name="fk_plan_settings",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "build_id", "deployment_id", "settings_hash", name="uq_plan_build_scope"
        ),
        sa.UniqueConstraint("build_id", "generation", name="uq_plan_build_generation"),
        sa.UniqueConstraint(
            "environment_id",
            "id",
            "deployment_id",
            "settings_hash",
            name="uq_plan_id_scope",
        ),
        sa.UniqueConstraint("environment_id", "id", name="uq_plan_environment_id"),
    )
    op.create_index(
        "uq_plan_build_active",
        "plan",
        ["build_id"],
        unique=True,
        postgresql_where=sa.text("activated_at IS NOT NULL AND superseded_at IS NULL"),
    )
    op.create_table(
        "task_artifact",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_pk", sa.Uuid(), nullable=False),
        sa.Column("artifact_type", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("body_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "task_pk"],
            ["task.environment_id", "task.id"],
            name="fk_task_artifact_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_pk", "artifact_type", "name", name="uq_task_artifact_task_type_name"
        ),
    )
    op.create_index(
        "ix_task_artifact_environment_created",
        "task_artifact",
        ["environment_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "task_instance",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("settings_hash", sa.Uuid(), nullable=False),
        sa.Column("instance_hash", sa.String(length=64), nullable=False),
        sa.Column("task_pk", sa.Uuid(), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expanded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "deployment_id"],
            ["deployment.environment_id", "deployment.id"],
            name="fk_task_instance_deployment",
            ondelete="NO ACTION",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "settings_hash"],
            ["settings.environment_id", "settings.hash"],
            name="fk_task_instance_settings",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "task_pk"],
            ["task.environment_id", "task.id"],
            name="fk_task_instance_task",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deployment_id",
            "settings_hash",
            "instance_hash",
            name="uq_task_instance_scope_hash",
        ),
        sa.UniqueConstraint(
            "environment_id",
            "id",
            "deployment_id",
            "settings_hash",
            name="uq_task_instance_id_scope",
        ),
        sa.UniqueConstraint(
            "environment_id", "id", "task_pk", name="uq_task_instance_id_task"
        ),
    )
    op.create_index(
        "ix_task_instance_scope_task",
        "task_instance",
        ["deployment_id", "settings_hash", "task_pk"],
        unique=False,
    )
    op.create_index(
        "ix_task_instance_environment_created",
        "task_instance",
        ["environment_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "task_limit_key",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_pk", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "task_pk"],
            ["task.environment_id", "task.id"],
            name="fk_task_limit_key_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_pk", "key", name="uq_task_limit_key_task_key"),
    )
    op.create_index(
        "ix_task_limit_key_environment_key",
        "task_limit_key",
        ["environment_id", "key"],
        unique=False,
    )
    op.create_table(
        "plan_member",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("task_pk", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("settings_hash", sa.Uuid(), nullable=False),
        sa.Column(
            "is_root", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "admitted_by",
            sa.Enum(
                "root", "static", "dynamic", "closure", name="plan_member_admitted_by"
            ),
            nullable=False,
        ),
        sa.Column("excluded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "excluded_reason",
            sa.Enum(
                "operator",
                "discovery_failed",
                "upstream_excluded",
                name="plan_member_exclusion_reason",
            ),
            nullable=True,
        ),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(excluded_at IS NULL) = (excluded_reason IS NULL)",
            name="ck_plan_member_exclusion_has_reason",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "instance_id", "deployment_id", "settings_hash"],
            [
                "task_instance.environment_id",
                "task_instance.id",
                "task_instance.deployment_id",
                "task_instance.settings_hash",
            ],
            name="fk_plan_member_instance_scope",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "instance_id", "task_pk"],
            [
                "task_instance.environment_id",
                "task_instance.id",
                "task_instance.task_pk",
            ],
            name="fk_plan_member_instance_task",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "plan_id", "deployment_id", "settings_hash"],
            [
                "plan.environment_id",
                "plan.id",
                "plan.deployment_id",
                "plan.settings_hash",
            ],
            name="fk_plan_member_plan_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "task_pk"],
            ["task.environment_id", "task.id"],
            name="fk_plan_member_task",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("plan_id", "task_pk", name="pk_plan_member"),
        sa.UniqueConstraint(
            "environment_id", "plan_id", "instance_id", name="uq_plan_member_instance"
        ),
        sa.UniqueConstraint(
            "environment_id", "plan_id", "task_pk", name="uq_plan_member_task"
        ),
    )
    op.create_index("ix_plan_member_task", "plan_member", ["task_pk"], unique=False)
    op.create_table(
        "task_instance_dependency",
        sa.Column("downstream_instance_id", sa.Uuid(), nullable=False),
        sa.Column("upstream_instance_id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("settings_hash", sa.Uuid(), nullable=False),
        sa.Column(
            "is_dynamic", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            [
                "environment_id",
                "downstream_instance_id",
                "deployment_id",
                "settings_hash",
            ],
            [
                "task_instance.environment_id",
                "task_instance.id",
                "task_instance.deployment_id",
                "task_instance.settings_hash",
            ],
            name="fk_task_instance_dependency_downstream",
        ),
        sa.ForeignKeyConstraint(
            [
                "environment_id",
                "upstream_instance_id",
                "deployment_id",
                "settings_hash",
            ],
            [
                "task_instance.environment_id",
                "task_instance.id",
                "task_instance.deployment_id",
                "task_instance.settings_hash",
            ],
            name="fk_task_instance_dependency_upstream",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "downstream_instance_id",
            "upstream_instance_id",
            name="pk_task_instance_dependency",
        ),
    )
    op.create_index(
        "ix_task_instance_dependency_upstream",
        "task_instance_dependency",
        ["upstream_instance_id"],
        unique=False,
    )
    op.create_table(
        "execution",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_pk", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("executor", sa.String(length=32), nullable=True),
        sa.Column("executor_ref", sa.String(length=255), nullable=True),
        sa.Column(
            "executor_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "claim_outcome",
            sa.Enum(
                "completed",
                "failed",
                "suspended",
                "interrupted",
                "cancelled",
                "taken_over",
                "lapsed",
                "released",
                name="execution_claim_outcome",
            ),
            nullable=True,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "outcome",
            sa.Enum(
                "completed",
                "failed",
                "suspended",
                "interrupted",
                "preempted",
                "stopped",
                "lost",
                name="execution_outcome",
            ),
            nullable=True,
        ),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(claim_released_at IS NULL) = (claim_outcome IS NULL)",
            name="ck_execution_claim_release_has_outcome",
        ),
        sa.CheckConstraint(
            "(ended_at IS NULL) = (outcome IS NULL)",
            name="ck_execution_end_has_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "instance_id", "task_pk"],
            [
                "task_instance.environment_id",
                "task_instance.id",
                "task_instance.task_pk",
            ],
            name="fk_execution_instance_task",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "plan_id", "instance_id"],
            [
                "plan_member.environment_id",
                "plan_member.plan_id",
                "plan_member.instance_id",
            ],
            name="fk_execution_plan_member",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "task_pk"],
            ["task.environment_id", "task.id"],
            name="fk_execution_task",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("environment_id", "id", name="uq_execution_environment_id"),
        sa.UniqueConstraint(
            "environment_id", "task_pk", "id", name="uq_execution_task_id"
        ),
    )
    op.create_index(
        "ix_execution_plan_instance",
        "execution",
        ["plan_id", "instance_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_task_started",
        "execution",
        ["task_pk", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_execution_task_plan",
        "execution",
        ["task_pk", "plan_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_unended",
        "execution",
        ["plan_id"],
        unique=False,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_table(
        "event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("build_id", sa.Uuid(), nullable=True),
        sa.Column("task_pk", sa.Uuid(), nullable=True),
        sa.Column("plan_id", sa.Uuid(), nullable=True),
        sa.Column("execution_id", sa.Uuid(), nullable=True),
        sa.Column(
            "event_type",
            sa.Enum(
                "build_started",
                "build_resumed",
                "build_completed",
                "build_failed",
                "build_cancelled",
                "build_exit_early",
                "task_pending",
                "task_referenced",
                "task_started",
                "task_suspended",
                "task_resumed",
                "task_retried",
                "task_completed",
                "task_failed",
                "task_interrupted",
                "task_preempted",
                "task_skipped",
                "task_cancelled",
                "task_invalidated",
                "task_excluded",
                "task_observed_complete",
                "task_structure_diverged",
                "task_yielded",
                name="event_type",
            ),
            nullable=False,
        ),
        sa.Column(
            "report_applied",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("batch_id", sa.Uuid(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "event_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "plan_id IS NULL OR event_type NOT IN ('build_started', 'build_resumed', 'build_completed', 'build_failed', 'build_cancelled', 'build_exit_early')",
            name="ck_event_build_level_has_no_plan",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "build_id"],
            ["build.environment_id", "build.id"],
            name="fk_event_build",
            ondelete="SET NULL (build_id)",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "execution_id"],
            ["execution.environment_id", "execution.id"],
            name="fk_event_execution",
            ondelete="SET NULL (execution_id)",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "plan_id"],
            ["plan.environment_id", "plan.id"],
            name="fk_event_plan",
            ondelete="SET NULL (plan_id)",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "task_pk"],
            ["task.environment_id", "task.id"],
            name="fk_event_task",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_event_build_created", "event", ["build_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_event_environment_created",
        "event",
        ["environment_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_event_execution", "event", ["execution_id"], unique=False)
    op.create_index("ix_event_plan", "event", ["plan_id"], unique=False)
    op.create_index(
        "ix_event_task_created", "event", ["task_pk", "created_at"], unique=False
    )
    op.create_index(
        "uq_event_execution_batch",
        "event",
        ["execution_id", "batch_id"],
        unique=True,
        postgresql_where=sa.text("batch_id IS NOT NULL"),
    )

    # The two pointer FKs on ``task``, circular with plan_member/execution.
    op.create_foreign_key(
        "fk_task_claim_plan_member",
        "task",
        "plan_member",
        ["environment_id", "claim_plan_id", "id"],
        ["environment_id", "plan_id", "task_pk"],
        ondelete="SET NULL (claim_plan_id)",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_task_execution",
        "task",
        "execution",
        ["environment_id", "id", "execution_id"],
        ["environment_id", "task_pk", "id"],
        ondelete="SET NULL (execution_id)",
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    """Not supported: v2 is a new line and v1 data is not reconstructible."""
    raise NotImplementedError(
        "registry v2 has no downgrade: the v1 core tables were dropped with "
        "their data, and there is nothing to rebuild them from."
    )
