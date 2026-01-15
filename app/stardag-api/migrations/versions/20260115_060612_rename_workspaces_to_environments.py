"""rename workspaces to environments

Revision ID: 7ea34c6fc27a
Revises: 43245b5116be
Create Date: 2026-01-15 06:06:12.756976

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7ea34c6fc27a"
down_revision: Union[str, Sequence[str], None] = "43245b5116be"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: rename workspaces to environments."""
    # Step 1: Drop foreign keys referencing workspaces table
    # (Must drop before renaming table)
    op.drop_constraint("api_keys_workspace_id_fkey", "api_keys", type_="foreignkey")
    op.drop_constraint("builds_workspace_id_fkey", "builds", type_="foreignkey")
    op.drop_constraint(
        "distributed_locks_workspace_id_fkey", "distributed_locks", type_="foreignkey"
    )
    op.drop_constraint(
        "target_roots_workspace_id_fkey", "target_roots", type_="foreignkey"
    )
    op.drop_constraint(
        "task_registry_assets_workspace_id_fkey",
        "task_registry_assets",
        type_="foreignkey",
    )
    op.drop_constraint("tasks_workspace_id_fkey", "tasks", type_="foreignkey")

    # Step 2: Drop old indexes on workspaces table
    op.drop_index("ix_workspaces_organization_id", table_name="workspaces")
    op.drop_index("ix_workspaces_owner_id", table_name="workspaces")
    op.drop_index("ix_workspaces_slug", table_name="workspaces")

    # Step 3: Drop old indexes on workspace_id columns
    op.drop_index("ix_api_keys_workspace_id", table_name="api_keys")
    op.drop_index("ix_builds_workspace_id", table_name="builds")
    op.drop_index("ix_builds_workspace_created", table_name="builds")
    op.drop_index(
        "ix_distributed_locks_workspace_expires", table_name="distributed_locks"
    )
    op.drop_index("ix_target_roots_workspace_id", table_name="target_roots")
    op.drop_index(
        "ix_task_registry_assets_workspace", table_name="task_registry_assets"
    )
    op.drop_index("ix_tasks_workspace_id", table_name="tasks")
    op.drop_index("ix_tasks_workspace_name", table_name="tasks")
    op.drop_index("ix_tasks_workspace_namespace", table_name="tasks")

    # Step 4: Drop old unique constraints
    op.drop_constraint("uq_workspace_org_slug", "workspaces", type_="unique")
    op.drop_constraint("uq_target_root_workspace_name", "target_roots", type_="unique")
    op.drop_constraint("uq_task_workspace_taskid", "tasks", type_="unique")

    # Step 5: Rename workspaces table to environments
    op.rename_table("workspaces", "environments")

    # Step 6: Rename workspace_id columns to environment_id
    op.alter_column("api_keys", "workspace_id", new_column_name="environment_id")
    op.alter_column("builds", "workspace_id", new_column_name="environment_id")
    op.alter_column(
        "distributed_locks", "workspace_id", new_column_name="environment_id"
    )
    op.alter_column("target_roots", "workspace_id", new_column_name="environment_id")
    op.alter_column(
        "task_registry_assets", "workspace_id", new_column_name="environment_id"
    )
    op.alter_column("tasks", "workspace_id", new_column_name="environment_id")

    # Step 7: Create new indexes on environments table
    op.create_index(
        "ix_environments_organization_id",
        "environments",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_environments_owner_id", "environments", ["owner_id"], unique=False
    )
    op.create_index("ix_environments_slug", "environments", ["slug"], unique=False)

    # Step 8: Create new indexes on environment_id columns
    op.create_index(
        "ix_api_keys_environment_id", "api_keys", ["environment_id"], unique=False
    )
    op.create_index(
        "ix_builds_environment_id", "builds", ["environment_id"], unique=False
    )
    op.create_index(
        "ix_builds_environment_created",
        "builds",
        ["environment_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_distributed_locks_environment_expires",
        "distributed_locks",
        ["environment_id", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_target_roots_environment_id",
        "target_roots",
        ["environment_id"],
        unique=False,
    )
    op.create_index(
        "ix_task_registry_assets_environment",
        "task_registry_assets",
        ["environment_id"],
        unique=False,
    )
    op.create_index(
        "ix_tasks_environment_id", "tasks", ["environment_id"], unique=False
    )
    op.create_index(
        "ix_tasks_environment_name",
        "tasks",
        ["environment_id", "task_name"],
        unique=False,
    )
    op.create_index(
        "ix_tasks_environment_namespace",
        "tasks",
        ["environment_id", "task_namespace"],
        unique=False,
    )

    # Step 9: Create new unique constraints
    op.create_unique_constraint(
        "uq_environment_org_slug", "environments", ["organization_id", "slug"]
    )
    op.create_unique_constraint(
        "uq_target_root_environment_name", "target_roots", ["environment_id", "name"]
    )
    op.create_unique_constraint(
        "uq_task_environment_taskid", "tasks", ["environment_id", "task_id"]
    )

    # Step 10: Recreate foreign keys pointing to environments table
    op.create_foreign_key(
        "api_keys_environment_id_fkey",
        "api_keys",
        "environments",
        ["environment_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "builds_environment_id_fkey",
        "builds",
        "environments",
        ["environment_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "distributed_locks_environment_id_fkey",
        "distributed_locks",
        "environments",
        ["environment_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "target_roots_environment_id_fkey",
        "target_roots",
        "environments",
        ["environment_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "task_registry_assets_environment_id_fkey",
        "task_registry_assets",
        "environments",
        ["environment_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "tasks_environment_id_fkey",
        "tasks",
        "environments",
        ["environment_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    """Downgrade schema: rename environments back to workspaces."""
    # Step 1: Drop foreign keys referencing environments table
    op.drop_constraint("api_keys_environment_id_fkey", "api_keys", type_="foreignkey")
    op.drop_constraint("builds_environment_id_fkey", "builds", type_="foreignkey")
    op.drop_constraint(
        "distributed_locks_environment_id_fkey", "distributed_locks", type_="foreignkey"
    )
    op.drop_constraint(
        "target_roots_environment_id_fkey", "target_roots", type_="foreignkey"
    )
    op.drop_constraint(
        "task_registry_assets_environment_id_fkey",
        "task_registry_assets",
        type_="foreignkey",
    )
    op.drop_constraint("tasks_environment_id_fkey", "tasks", type_="foreignkey")

    # Step 2: Drop new indexes on environments table
    op.drop_index("ix_environments_organization_id", table_name="environments")
    op.drop_index("ix_environments_owner_id", table_name="environments")
    op.drop_index("ix_environments_slug", table_name="environments")

    # Step 3: Drop new indexes on environment_id columns
    op.drop_index("ix_api_keys_environment_id", table_name="api_keys")
    op.drop_index("ix_builds_environment_id", table_name="builds")
    op.drop_index("ix_builds_environment_created", table_name="builds")
    op.drop_index(
        "ix_distributed_locks_environment_expires", table_name="distributed_locks"
    )
    op.drop_index("ix_target_roots_environment_id", table_name="target_roots")
    op.drop_index(
        "ix_task_registry_assets_environment", table_name="task_registry_assets"
    )
    op.drop_index("ix_tasks_environment_id", table_name="tasks")
    op.drop_index("ix_tasks_environment_name", table_name="tasks")
    op.drop_index("ix_tasks_environment_namespace", table_name="tasks")

    # Step 4: Drop new unique constraints
    op.drop_constraint("uq_environment_org_slug", "environments", type_="unique")
    op.drop_constraint(
        "uq_target_root_environment_name", "target_roots", type_="unique"
    )
    op.drop_constraint("uq_task_environment_taskid", "tasks", type_="unique")

    # Step 5: Rename environments table back to workspaces
    op.rename_table("environments", "workspaces")

    # Step 6: Rename environment_id columns back to workspace_id
    op.alter_column("api_keys", "environment_id", new_column_name="workspace_id")
    op.alter_column("builds", "environment_id", new_column_name="workspace_id")
    op.alter_column(
        "distributed_locks", "environment_id", new_column_name="workspace_id"
    )
    op.alter_column("target_roots", "environment_id", new_column_name="workspace_id")
    op.alter_column(
        "task_registry_assets", "environment_id", new_column_name="workspace_id"
    )
    op.alter_column("tasks", "environment_id", new_column_name="workspace_id")

    # Step 7: Recreate old indexes on workspaces table
    op.create_index(
        "ix_workspaces_organization_id", "workspaces", ["organization_id"], unique=False
    )
    op.create_index("ix_workspaces_owner_id", "workspaces", ["owner_id"], unique=False)
    op.create_index("ix_workspaces_slug", "workspaces", ["slug"], unique=False)

    # Step 8: Recreate old indexes on workspace_id columns
    op.create_index(
        "ix_api_keys_workspace_id", "api_keys", ["workspace_id"], unique=False
    )
    op.create_index("ix_builds_workspace_id", "builds", ["workspace_id"], unique=False)
    op.create_index(
        "ix_builds_workspace_created",
        "builds",
        ["workspace_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_distributed_locks_workspace_expires",
        "distributed_locks",
        ["workspace_id", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_target_roots_workspace_id", "target_roots", ["workspace_id"], unique=False
    )
    op.create_index(
        "ix_task_registry_assets_workspace",
        "task_registry_assets",
        ["workspace_id"],
        unique=False,
    )
    op.create_index("ix_tasks_workspace_id", "tasks", ["workspace_id"], unique=False)
    op.create_index(
        "ix_tasks_workspace_name", "tasks", ["workspace_id", "task_name"], unique=False
    )
    op.create_index(
        "ix_tasks_workspace_namespace",
        "tasks",
        ["workspace_id", "task_namespace"],
        unique=False,
    )

    # Step 9: Recreate old unique constraints
    op.create_unique_constraint(
        "uq_workspace_org_slug", "workspaces", ["organization_id", "slug"]
    )
    op.create_unique_constraint(
        "uq_target_root_workspace_name", "target_roots", ["workspace_id", "name"]
    )
    op.create_unique_constraint(
        "uq_task_workspace_taskid", "tasks", ["workspace_id", "task_id"]
    )

    # Step 10: Recreate foreign keys pointing to workspaces table
    op.create_foreign_key(
        "api_keys_workspace_id_fkey",
        "api_keys",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "builds_workspace_id_fkey",
        "builds",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "distributed_locks_workspace_id_fkey",
        "distributed_locks",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "target_roots_workspace_id_fkey",
        "target_roots",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "task_registry_assets_workspace_id_fkey",
        "task_registry_assets",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "tasks_workspace_id_fkey",
        "tasks",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )
