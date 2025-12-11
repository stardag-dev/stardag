"""Normalized schema with Organization, Workspace, User, Run, Task, Event, TaskDependency.

Revision ID: 001_normalized
Revises:
Create Date: 2025-12-11 22:00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001_normalized"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create normalized schema."""
    # Organizations
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), unique=True, nullable=False, index=True),
        sa.Column("slug", sa.String(64), unique=True, nullable=False, index=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Users
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(36),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("username", sa.String(255), nullable=False, index=True),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("organization_id", "username", name="uq_user_org_username"),
    )

    # Workspaces
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(36),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False, index=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("organization_id", "slug", name="uq_workspace_org_slug"),
    )

    # Runs
    op.create_table(
        "runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("name", sa.String(64), nullable=False, index=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("commit_hash", sa.String(64), nullable=True, index=True),
        sa.Column("root_task_ids", sa.JSON, nullable=False, server_default="[]"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_runs_workspace_created", "runs", ["workspace_id", "created_at"])

    # Tasks
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(64), nullable=False, index=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("task_namespace", sa.String(255), nullable=False, server_default=""),
        sa.Column("task_family", sa.String(255), nullable=False, index=True),
        sa.Column("task_data", sa.JSON, nullable=False),
        sa.Column("version", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("workspace_id", "task_id", name="uq_task_workspace_taskid"),
    )
    op.create_index(
        "ix_tasks_workspace_family", "tasks", ["workspace_id", "task_family"]
    )
    op.create_index(
        "ix_tasks_workspace_namespace", "tasks", ["workspace_id", "task_namespace"]
    )

    # Task Dependencies
    op.create_table(
        "task_dependencies",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "upstream_task_id",
            sa.Integer,
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "downstream_task_id",
            sa.Integer,
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "upstream_task_id", "downstream_task_id", name="uq_task_dependency_edge"
        ),
    )
    op.create_index("ix_task_dep_upstream", "task_dependencies", ["upstream_task_id"])
    op.create_index(
        "ix_task_dep_downstream", "task_dependencies", ["downstream_task_id"]
    )

    # Events
    op.create_table(
        "events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "task_id",
            sa.Integer,
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column("event_type", sa.String(32), nullable=False, index=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("event_metadata", sa.JSON, nullable=True),
    )
    op.create_index("ix_events_run_created", "events", ["run_id", "created_at"])
    op.create_index("ix_events_task_created", "events", ["task_id", "created_at"])
    op.create_index("ix_events_type_created", "events", ["event_type", "created_at"])
    op.create_index(
        "ix_events_run_task_type", "events", ["run_id", "task_id", "event_type"]
    )

    # Seed default organization, workspace, and user
    op.execute(
        """
        INSERT INTO organizations (id, name, slug)
        VALUES ('default', 'Default Organization', 'default')
        """
    )
    op.execute(
        """
        INSERT INTO users (id, organization_id, username, display_name)
        VALUES ('default', 'default', 'default', 'Default User')
        """
    )
    op.execute(
        """
        INSERT INTO workspaces (id, organization_id, name, slug)
        VALUES ('default', 'default', 'Default Workspace', 'default')
        """
    )


def downgrade() -> None:
    """Drop all tables."""
    op.drop_table("events")
    op.drop_table("task_dependencies")
    op.drop_table("tasks")
    op.drop_table("runs")
    op.drop_table("workspaces")
    op.drop_table("users")
    op.drop_table("organizations")
