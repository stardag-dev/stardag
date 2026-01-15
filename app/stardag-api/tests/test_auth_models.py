"""Tests for auth-related models (WorkspaceMember, Invite, ApiKey)."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    ApiKey,
    Invite,
    InviteStatus,
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
    User,
)


class TestWorkspaceMember:
    """Tests for WorkspaceMember model."""

    async def test_create_membership(self, async_session: AsyncSession):
        """Test creating a membership relationship."""
        # Create a new workspace and user
        workspace = Workspace(name="Test Workspace", slug="test-workspace")
        user = User(external_id="user-123", email="user@test.com", display_name="Test")
        async_session.add_all([workspace, user])
        await async_session.flush()

        # Create membership
        membership = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.ADMIN,
        )
        async_session.add(membership)
        await async_session.commit()

        # Verify
        result = await async_session.execute(
            select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)
        )
        saved = result.scalar_one()
        assert saved.role == WorkspaceRole.ADMIN
        assert saved.workspace_id == workspace.id

    async def test_unique_membership_constraint(self, async_session: AsyncSession):
        """Test that user can only have one membership per workspace."""
        workspace = Workspace(name="Unique Workspace", slug="unique-workspace")
        user = User(external_id="unique-user", email="unique@test.com")
        async_session.add_all([workspace, user])
        await async_session.flush()

        # First membership
        m1 = WorkspaceMember(
            workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.MEMBER
        )
        async_session.add(m1)
        await async_session.commit()

        # Duplicate should fail
        m2 = WorkspaceMember(
            workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.ADMIN
        )
        async_session.add(m2)
        with pytest.raises(IntegrityError):
            await async_session.commit()

    async def test_user_multiple_workspaces(self, async_session: AsyncSession):
        """Test that a user can belong to multiple workspaces."""
        workspace1 = Workspace(name="Workspace One", slug="workspace-one")
        workspace2 = Workspace(name="Workspace Two", slug="workspace-two")
        user = User(external_id="multi-workspace-user", email="multi@test.com")
        async_session.add_all([workspace1, workspace2, user])
        await async_session.flush()

        m1 = WorkspaceMember(
            workspace_id=workspace1.id, user_id=user.id, role=WorkspaceRole.OWNER
        )
        m2 = WorkspaceMember(
            workspace_id=workspace2.id, user_id=user.id, role=WorkspaceRole.MEMBER
        )
        async_session.add_all([m1, m2])
        await async_session.commit()

        # Verify user has two memberships
        result = await async_session.execute(
            select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)
        )
        memberships = result.scalars().all()
        assert len(memberships) == 2


class TestInvite:
    """Tests for Invite model."""

    async def test_create_invite(self, async_session: AsyncSession):
        """Test creating an invite."""
        # Use default workspace from fixtures
        invite = Invite(
            workspace_id="default",
            email="newuser@test.com",
            role=WorkspaceRole.MEMBER,
            invited_by_id="default",
        )
        async_session.add(invite)
        await async_session.commit()

        result = await async_session.execute(
            select(Invite).where(Invite.email == "newuser@test.com")
        )
        saved = result.scalar_one()
        assert saved.status == InviteStatus.PENDING
        assert saved.role == WorkspaceRole.MEMBER

    async def test_invite_status_transitions(self, async_session: AsyncSession):
        """Test invite status can be changed."""
        invite = Invite(
            workspace_id="default",
            email="status@test.com",
            role=WorkspaceRole.ADMIN,
        )
        async_session.add(invite)
        await async_session.commit()

        # Accept the invite
        invite.status = InviteStatus.ACCEPTED
        await async_session.commit()

        result = await async_session.execute(
            select(Invite).where(Invite.id == invite.id)
        )
        saved = result.scalar_one()
        assert saved.status == InviteStatus.ACCEPTED


class TestApiKey:
    """Tests for ApiKey model."""

    async def test_create_api_key(self, async_session: AsyncSession):
        """Test creating an API key."""
        api_key = ApiKey(
            environment_id="default",
            name="Test Key",
            key_prefix="sk_test_",
            key_hash="$2b$12$fakehash",
            created_by_id="default",
        )
        async_session.add(api_key)
        await async_session.commit()

        result = await async_session.execute(
            select(ApiKey).where(ApiKey.name == "Test Key")
        )
        saved = result.scalar_one()
        assert saved.key_prefix == "sk_test_"
        assert saved.is_active is True
        assert saved.revoked_at is None

    async def test_api_key_revocation(self, async_session: AsyncSession):
        """Test revoking an API key."""
        from datetime import datetime, timezone

        api_key = ApiKey(
            environment_id="default",
            name="Revocable Key",
            key_prefix="sk_rev_",
            key_hash="$2b$12$fakehash2",
        )
        async_session.add(api_key)
        await async_session.commit()

        assert api_key.is_active is True

        # Revoke
        api_key.revoked_at = datetime.now(timezone.utc)
        await async_session.commit()

        result = await async_session.execute(
            select(ApiKey).where(ApiKey.id == api_key.id)
        )
        saved = result.scalar_one()
        assert saved.is_active is False


class TestUser:
    """Tests for updated User model."""

    async def test_user_unique_external_id(self, async_session: AsyncSession):
        """Test that external_id must be unique."""
        user1 = User(external_id="same-ext-id", email="user1@test.com")
        async_session.add(user1)
        await async_session.commit()

        user2 = User(external_id="same-ext-id", email="user2@test.com")
        async_session.add(user2)
        with pytest.raises(IntegrityError):
            await async_session.commit()

    async def test_user_unique_email(self, async_session: AsyncSession):
        """Test that email must be unique."""
        user1 = User(external_id="ext-1", email="same@test.com")
        async_session.add(user1)
        await async_session.commit()

        user2 = User(external_id="ext-2", email="same@test.com")
        async_session.add(user2)
        with pytest.raises(IntegrityError):
            await async_session.commit()
