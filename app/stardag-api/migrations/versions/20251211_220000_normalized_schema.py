"""Normalized schema with multi-tenant auth support.

Includes Organization, OrganizationMember, User, Workspace, Build, Task,
Event, TaskDependency, Invite, and ApiKey tables.

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
    """Create normalized schema with auth tables."""
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

    # Users (no org FK - users belong to orgs via organization_members)
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "external_id", sa.String(255), unique=True, nullable=False, index=True
        ),
        sa.Column("email", sa.String(255), unique=True, nullable=False, index=True),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Organization Members (junction table with roles)
    op.create_table(
        "organization_members",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(36),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "role",
            sa.Enum("owner", "admin", "member", name="organizationrole"),
            nullable=False,
            server_default="member",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("organization_id", "user_id", name="uq_org_member"),
    )

    # Invites
    op.create_table(
        "invites",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(36),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("email", sa.String(255), nullable=False, index=True),
        sa.Column(
            "role",
            sa.Enum("owner", "admin", "member", name="organizationrole"),
            nullable=False,
            server_default="member",
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "accepted", "declined", "cancelled", name="invitestatus"
            ),
            nullable=False,
            server_default="pending",
            index=True,
        ),
        sa.Column(
            "invited_by_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Partial unique index for pending invites (PostgreSQL specific)
    op.execute(
        """
        CREATE UNIQUE INDEX ix_invite_org_email_pending
        ON invites (organization_id, email)
        WHERE status = 'pending'
        """
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

    # API Keys (scoped to workspace)
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key_prefix", sa.String(8), nullable=False, index=True),
        sa.Column("key_hash", sa.String(255), nullable=False),
        sa.Column(
            "created_by_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Builds
    op.create_table(
        "builds",
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
    op.create_index(
        "ix_builds_workspace_created", "builds", ["workspace_id", "created_at"]
    )

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
            "build_id",
            sa.String(36),
            sa.ForeignKey("builds.id", ondelete="CASCADE"),
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
    op.create_index("ix_events_build_created", "events", ["build_id", "created_at"])
    op.create_index("ix_events_task_created", "events", ["task_id", "created_at"])
    op.create_index("ix_events_type_created", "events", ["event_type", "created_at"])
    op.create_index(
        "ix_events_build_task_type", "events", ["build_id", "task_id", "event_type"]
    )

    # Seed default organization, user, workspace, and membership for local dev
    # In production, users will be created via OIDC login
    op.execute(
        """
        INSERT INTO organizations (id, name, slug)
        VALUES ('default', 'Default Organization', 'default')
        """
    )
    op.execute(
        """
        INSERT INTO users (id, external_id, email, display_name)
        VALUES ('default', 'default-local-user', 'default@localhost', 'Default User')
        """
    )
    op.execute(
        """
        INSERT INTO organization_members (id, organization_id, user_id, role)
        VALUES ('default', 'default', 'default', 'owner')
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
    op.drop_table("builds")
    op.drop_table("api_keys")
    op.drop_table("workspaces")
    op.execute("DROP INDEX IF EXISTS ix_invite_org_email_pending")
    op.drop_table("invites")
    op.drop_table("organization_members")
    op.drop_table("users")
    op.drop_table("organizations")
    op.execute("DROP TYPE IF EXISTS organizationrole")
    op.execute("DROP TYPE IF EXISTS invitestatus")
