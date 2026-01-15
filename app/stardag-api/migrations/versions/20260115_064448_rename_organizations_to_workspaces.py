"""rename organizations to workspaces

Revision ID: 467b15c7fd1d
Revises: 7ea34c6fc27a
Create Date: 2026-01-15 06:44:48.394287

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "467b15c7fd1d"
down_revision: Union[str, Sequence[str], None] = "7ea34c6fc27a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: rename organizations to workspaces."""
    # Step 1: Create the new workspacerole enum
    op.execute("CREATE TYPE workspacerole AS ENUM ('owner', 'admin', 'member')")

    # Step 2: Rename organizations table to workspaces
    op.rename_table("organizations", "workspaces")

    # Step 3: Rename organization_members table to workspace_members
    op.rename_table("organization_members", "workspace_members")

    # Step 4: Rename organization_id column to workspace_id in workspace_members
    op.alter_column(
        "workspace_members", "organization_id", new_column_name="workspace_id"
    )

    # Step 5: Rename organization_id column to workspace_id in environments
    # Note: The FK constraint on environments is named workspaces_organization_id_fkey
    # (historical name from when environments was called workspaces)
    op.alter_column("environments", "organization_id", new_column_name="workspace_id")

    # Step 6: Rename organization_id column to workspace_id in invites
    op.alter_column("invites", "organization_id", new_column_name="workspace_id")

    # Step 7: Change role column type in invites from organizationrole to workspacerole
    op.execute(
        "ALTER TABLE invites ALTER COLUMN role TYPE workspacerole USING role::text::workspacerole"
    )

    # Step 8: Change role column type in workspace_members from organizationrole to workspacerole
    op.execute(
        "ALTER TABLE workspace_members ALTER COLUMN role TYPE workspacerole USING role::text::workspacerole"
    )

    # Step 9: Drop the old organizationrole enum
    op.execute("DROP TYPE organizationrole")

    # Step 10: Rename indexes on workspaces table (from organizations)
    op.execute(
        "ALTER INDEX ix_organizations_created_by_id RENAME TO ix_workspaces_created_by_id"
    )
    op.execute("ALTER INDEX ix_organizations_name RENAME TO ix_workspaces_name")
    op.execute("ALTER INDEX ix_organizations_slug RENAME TO ix_workspaces_slug")

    # Step 11: Rename indexes on workspace_members table
    op.execute(
        "ALTER INDEX ix_organization_members_organization_id RENAME TO ix_workspace_members_workspace_id"
    )
    op.execute(
        "ALTER INDEX ix_organization_members_user_id RENAME TO ix_workspace_members_user_id"
    )

    # Step 12: Rename index on environments table
    op.execute(
        "ALTER INDEX ix_environments_organization_id RENAME TO ix_environments_workspace_id"
    )

    # Step 13: Rename index on invites table
    op.execute(
        "ALTER INDEX ix_invites_organization_id RENAME TO ix_invites_workspace_id"
    )
    op.execute(
        "ALTER INDEX ix_invite_org_email_pending RENAME TO ix_invite_workspace_email_pending"
    )

    # Step 14: Rename unique constraints
    op.execute(
        "ALTER TABLE workspace_members RENAME CONSTRAINT uq_org_member TO uq_workspace_member"
    )
    op.execute(
        "ALTER TABLE environments RENAME CONSTRAINT uq_environment_org_slug TO uq_environment_workspace_slug"
    )

    # Step 15: Rename foreign key constraints
    # Note: environments table FK was named workspaces_organization_id_fkey from old table name
    op.execute(
        "ALTER TABLE workspace_members RENAME CONSTRAINT organization_members_organization_id_fkey TO workspace_members_workspace_id_fkey"
    )
    op.execute(
        "ALTER TABLE workspace_members RENAME CONSTRAINT organization_members_user_id_fkey TO workspace_members_user_id_fkey"
    )
    op.execute(
        "ALTER TABLE environments RENAME CONSTRAINT workspaces_organization_id_fkey TO environments_workspace_id_fkey"
    )
    op.execute(
        "ALTER TABLE invites RENAME CONSTRAINT invites_organization_id_fkey TO invites_workspace_id_fkey"
    )


def downgrade() -> None:
    """Downgrade schema: rename workspaces back to organizations."""
    # Step 1: Create the old organizationrole enum
    op.execute("CREATE TYPE organizationrole AS ENUM ('owner', 'admin', 'member')")

    # Step 2: Rename foreign key constraints back
    op.execute(
        "ALTER TABLE invites RENAME CONSTRAINT invites_workspace_id_fkey TO invites_organization_id_fkey"
    )
    op.execute(
        "ALTER TABLE environments RENAME CONSTRAINT environments_workspace_id_fkey TO workspaces_organization_id_fkey"
    )
    op.execute(
        "ALTER TABLE workspace_members RENAME CONSTRAINT workspace_members_user_id_fkey TO organization_members_user_id_fkey"
    )
    op.execute(
        "ALTER TABLE workspace_members RENAME CONSTRAINT workspace_members_workspace_id_fkey TO organization_members_organization_id_fkey"
    )

    # Step 3: Rename unique constraints back
    op.execute(
        "ALTER TABLE environments RENAME CONSTRAINT uq_environment_workspace_slug TO uq_environment_org_slug"
    )
    op.execute(
        "ALTER TABLE workspace_members RENAME CONSTRAINT uq_workspace_member TO uq_org_member"
    )

    # Step 4: Rename indexes on invites table back
    op.execute(
        "ALTER INDEX ix_invite_workspace_email_pending RENAME TO ix_invite_org_email_pending"
    )
    op.execute(
        "ALTER INDEX ix_invites_workspace_id RENAME TO ix_invites_organization_id"
    )

    # Step 5: Rename index on environments table back
    op.execute(
        "ALTER INDEX ix_environments_workspace_id RENAME TO ix_environments_organization_id"
    )

    # Step 6: Rename indexes on workspace_members table back
    op.execute(
        "ALTER INDEX ix_workspace_members_user_id RENAME TO ix_organization_members_user_id"
    )
    op.execute(
        "ALTER INDEX ix_workspace_members_workspace_id RENAME TO ix_organization_members_organization_id"
    )

    # Step 7: Rename indexes on workspaces table back
    op.execute("ALTER INDEX ix_workspaces_slug RENAME TO ix_organizations_slug")
    op.execute("ALTER INDEX ix_workspaces_name RENAME TO ix_organizations_name")
    op.execute(
        "ALTER INDEX ix_workspaces_created_by_id RENAME TO ix_organizations_created_by_id"
    )

    # Step 8: Change role column type back in workspace_members
    op.execute(
        "ALTER TABLE workspace_members ALTER COLUMN role TYPE organizationrole USING role::text::organizationrole"
    )

    # Step 9: Change role column type back in invites
    op.execute(
        "ALTER TABLE invites ALTER COLUMN role TYPE organizationrole USING role::text::organizationrole"
    )

    # Step 10: Drop the workspacerole enum
    op.execute("DROP TYPE workspacerole")

    # Step 11: Rename workspace_id column back to organization_id in invites
    op.alter_column("invites", "workspace_id", new_column_name="organization_id")

    # Step 12: Rename workspace_id column back to organization_id in environments
    op.alter_column("environments", "workspace_id", new_column_name="organization_id")

    # Step 13: Rename workspace_id column back to organization_id in workspace_members
    op.alter_column(
        "workspace_members", "workspace_id", new_column_name="organization_id"
    )

    # Step 14: Rename workspace_members table back to organization_members
    op.rename_table("workspace_members", "organization_members")

    # Step 15: Rename workspaces table back to organizations
    op.rename_table("workspaces", "organizations")
