"""Tests for workspace management routes."""

from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from stardag_api.auth.dependencies import get_current_user_flexible, get_token
from stardag_api.auth.tokens import InternalTokenPayload
from stardag_api.db import get_db
from stardag_api.main import app
from stardag_api.models import Environment, Workspace, WorkspaceMember, User
from stardag_api.models.enums import WorkspaceRole


def _create_mock_internal_token(
    user_id: UUID,
    workspace_id: UUID,
) -> InternalTokenPayload:
    """Create a mock internal token payload for testing.

    Internal tokens use the internal user ID (not external_id from OIDC).
    """
    return InternalTokenPayload(
        sub=str(user_id),
        workspace_id=str(workspace_id),
        iss="stardag-api",
        aud="stardag",
        exp=9999999999,
        iat=1234567800,
    )


@pytest.fixture
async def test_user_with_workspace(
    async_session: AsyncSession,
) -> tuple[User, Workspace]:
    """Create a test user with their own workspace.

    Returns (user, workspace) tuple. The user is owner of the workspace.
    """
    # Create user
    user = User(
        external_id="test-workspace-user",
        email="workspacetest@example.com",
        display_name="Workspace Test User",
    )
    async_session.add(user)
    await async_session.flush()  # Get user.id

    # Create workspace
    workspace = Workspace(
        name="Test Workspace",
        slug="test-workspace",
        description="A test workspace",
    )
    async_session.add(workspace)
    await async_session.flush()  # Get workspace.id

    # Create membership
    membership = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=user.id,
        role=WorkspaceRole.OWNER,
    )
    async_session.add(membership)

    # Create default environment
    environment = Environment(
        workspace_id=workspace.id,
        name="Default",
        slug="default",
    )
    async_session.add(environment)

    await async_session.commit()
    await async_session.refresh(user)
    await async_session.refresh(workspace)

    return user, workspace


@pytest.fixture
async def test_user(test_user_with_workspace: tuple[User, Workspace]) -> User:
    """Get the test user (for backwards compatibility with tests)."""
    return test_user_with_workspace[0]


@pytest.fixture
async def test_workspace_with_owner(
    test_user_with_workspace: tuple[User, Workspace],
) -> Workspace:
    """Get the test workspace (for backwards compatibility with tests)."""
    return test_user_with_workspace[1]


@pytest.fixture
def mock_token_for_user(test_user_with_workspace: tuple[User, Workspace]):
    """Create a mock internal token for the test user."""
    user, workspace = test_user_with_workspace
    return _create_mock_internal_token(
        user_id=user.id,
        workspace_id=workspace.id,
    )


@pytest.fixture
async def authenticated_client(async_engine, mock_token_for_user):
    """Create an authenticated test client."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token_for_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client

    app.dependency_overrides.clear()


# --- Workspace CRUD tests ---


@pytest.mark.asyncio
async def test_create_workspace(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test creating a new workspace.

    Note: Creating a workspace is a bootstrap endpoint that accepts Keycloak tokens
    (not internal tokens).
    """
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_current_user_flexible():
        return test_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_flexible] = (
        override_get_current_user_flexible
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/ui/workspaces",
                json={
                    "name": "New Workspace",
                    "slug": "new-workspace",
                    "description": "A new workspace",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "New Workspace"
        assert data["slug"] == "new-workspace"
        assert data["description"] == "A new workspace"
        assert "id" in data
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_workspace_duplicate_slug(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test that duplicate slugs are rejected.

    Note: Creating a workspace is a bootstrap endpoint that accepts Keycloak tokens
    (not internal tokens).
    """
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_current_user_flexible():
        return test_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_flexible] = (
        override_get_current_user_flexible
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/ui/workspaces",
                json={
                    "name": "Another Workspace",
                    "slug": "test-workspace",  # Same as test_workspace_with_owner
                },
            )

        assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_workspace(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test getting workspace details."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        workspace_id=test_workspace_with_owner.id,
    )

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                f"/api/v1/ui/workspaces/{test_workspace_with_owner.id}"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Test Workspace"
        assert data["member_count"] == 1
        assert data["environment_count"] == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_workspace_not_member(
    async_engine, test_workspace_with_owner: Workspace
):
    """Test that users can't access workspaces they're not a member of."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    # Create a different user with their own workspace
    async with async_session_maker() as session:
        other_user = User(
            external_id="other-user",
            email="other@example.com",
            display_name="Other User",
        )
        session.add(other_user)
        await session.flush()

        other_workspace = Workspace(
            name="Other Workspace",
            slug="other-workspace",
        )
        session.add(other_workspace)
        await session.flush()

        membership = WorkspaceMember(
            workspace_id=other_workspace.id,
            user_id=other_user.id,
            role=WorkspaceRole.OWNER,
        )
        session.add(membership)
        await session.commit()
        await session.refresh(other_user)
        await session.refresh(other_workspace)

    # Token for other_user scoped to their own workspace
    mock_token = _create_mock_internal_token(
        user_id=other_user.id,
        workspace_id=other_workspace.id,
    )

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                f"/api/v1/ui/workspaces/{test_workspace_with_owner.id}"
            )

        assert response.status_code == 404  # Not found (rather than 403)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_update_workspace(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test updating workspace."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        workspace_id=test_workspace_with_owner.id,
    )

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.patch(
                f"/api/v1/ui/workspaces/{test_workspace_with_owner.id}",
                json={"name": "Updated Name"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Name"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_workspace(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test deleting workspace (owner only)."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        workspace_id=test_workspace_with_owner.id,
    )

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.delete(
                f"/api/v1/ui/workspaces/{test_workspace_with_owner.id}"
            )

        assert response.status_code == 204
    finally:
        app.dependency_overrides.clear()


# --- Environment tests ---


@pytest.mark.asyncio
async def test_list_environments(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test listing environments."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        workspace_id=test_workspace_with_owner.id,
    )

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                f"/api/v1/ui/workspaces/{test_workspace_with_owner.id}/environments"
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["slug"] == "default"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_environment(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test creating an environment."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        workspace_id=test_workspace_with_owner.id,
    )

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/ui/workspaces/{test_workspace_with_owner.id}/environments",
                json={
                    "name": "New Environment",
                    "slug": "new-environment",
                    "description": "A new environment",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "New Environment"
        assert data["slug"] == "new-environment"
    finally:
        app.dependency_overrides.clear()


# --- Member tests ---


@pytest.mark.asyncio
async def test_list_members(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test listing workspace members."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        workspace_id=test_workspace_with_owner.id,
    )

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                f"/api/v1/ui/workspaces/{test_workspace_with_owner.id}/members"
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["role"] == "owner"
        assert data[0]["email"] == test_user.email
    finally:
        app.dependency_overrides.clear()


# --- Invite tests ---


@pytest.mark.asyncio
async def test_create_invite(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test creating an invite."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        workspace_id=test_workspace_with_owner.id,
    )

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/ui/workspaces/{test_workspace_with_owner.id}/invites",
                json={
                    "email": "invited@example.com",
                    "role": "member",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["email"] == "invited@example.com"
        assert data["role"] == "member"
        assert data["status"] == "pending"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_accept_invite(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test accepting an invite."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    # Create another user with their own workspace, and an invite for them
    async with async_session_maker() as session:
        invited_user = User(
            external_id="invited-user",
            email="invited@example.com",
            display_name="Invited User",
        )
        session.add(invited_user)
        await session.flush()

        # Create a personal workspace for the invited user (as would happen on first login)
        invited_user_workspace = Workspace(
            name="Invited User's Workspace",
            slug="invited-user-workspace",
        )
        session.add(invited_user_workspace)
        await session.flush()

        invited_user_membership = WorkspaceMember(
            workspace_id=invited_user_workspace.id,
            user_id=invited_user.id,
            role=WorkspaceRole.OWNER,
        )
        session.add(invited_user_membership)
        await session.commit()
        await session.refresh(invited_user)
        await session.refresh(invited_user_workspace)

        from stardag_api.models import Invite, InviteStatus

        invite = Invite(
            workspace_id=test_workspace_with_owner.id,
            email=invited_user.email,
            role=WorkspaceRole.MEMBER,
            invited_by_id=test_user.id,
            status=InviteStatus.PENDING,
        )
        session.add(invite)
        await session.commit()
        await session.refresh(invite)
        invite_id = invite.id

    # Now the invited user accepts (authenticated via Keycloak token - bootstrap endpoint)
    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_current_user_flexible():
        return invited_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_flexible] = (
        override_get_current_user_flexible
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/ui/workspaces/invites/{invite_id}/accept"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(test_workspace_with_owner.id)
    finally:
        app.dependency_overrides.clear()


# --- Target Root tests ---


@pytest.mark.asyncio
async def test_create_target_root(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test creating a target root in an environment."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        workspace_id=test_workspace_with_owner.id,
    )

    # Get the environment ID
    async with async_session_maker() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(Environment).where(
                Environment.workspace_id == test_workspace_with_owner.id
            )
        )
        environment = result.scalar_one()
        environment_id = environment.id

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/ui/workspaces/{test_workspace_with_owner.id}/environments/{environment_id}/target-roots",
                json={
                    "name": "default",
                    "uri_prefix": "s3://my-bucket/stardag/",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "default"
        assert data["uri_prefix"] == "s3://my-bucket/stardag/"
        assert data["environment_id"] == str(environment_id)
        assert "id" in data
        assert "created_at" in data
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_target_roots(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test listing target roots in an environment."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        workspace_id=test_workspace_with_owner.id,
    )

    # Get the environment ID and create a target root
    from stardag_api.models import TargetRoot

    async with async_session_maker() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(Environment).where(
                Environment.workspace_id == test_workspace_with_owner.id
            )
        )
        environment = result.scalar_one()
        environment_id = environment.id

        target_root = TargetRoot(
            environment_id=environment_id,
            name="test-root",
            uri_prefix="/data/stardag/",
        )
        session.add(target_root)
        await session.commit()

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                f"/api/v1/ui/workspaces/{test_workspace_with_owner.id}/environments/{environment_id}/target-roots"
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "test-root"
        assert data[0]["uri_prefix"] == "/data/stardag/"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_update_target_root(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test updating a target root."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        workspace_id=test_workspace_with_owner.id,
    )

    # Get the environment ID and create a target root
    from stardag_api.models import TargetRoot

    async with async_session_maker() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(Environment).where(
                Environment.workspace_id == test_workspace_with_owner.id
            )
        )
        environment = result.scalar_one()
        environment_id = environment.id

        target_root = TargetRoot(
            environment_id=environment_id,
            name="old-name",
            uri_prefix="/old/path/",
        )
        session.add(target_root)
        await session.commit()
        await session.refresh(target_root)
        root_id = target_root.id

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.patch(
                f"/api/v1/ui/workspaces/{test_workspace_with_owner.id}/environments/{environment_id}/target-roots/{root_id}",
                json={
                    "name": "new-name",
                    "uri_prefix": "/new/path/",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "new-name"
        assert data["uri_prefix"] == "/new/path/"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_target_root(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test deleting a target root."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        workspace_id=test_workspace_with_owner.id,
    )

    # Get the environment ID and create a target root
    from stardag_api.models import TargetRoot

    async with async_session_maker() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(Environment).where(
                Environment.workspace_id == test_workspace_with_owner.id
            )
        )
        environment = result.scalar_one()
        environment_id = environment.id

        target_root = TargetRoot(
            environment_id=environment_id,
            name="to-delete",
            uri_prefix="/delete/me/",
        )
        session.add(target_root)
        await session.commit()
        await session.refresh(target_root)
        root_id = target_root.id

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Delete the target root
            response = await client.delete(
                f"/api/v1/ui/workspaces/{test_workspace_with_owner.id}/environments/{environment_id}/target-roots/{root_id}"
            )
            assert response.status_code == 204

            # Verify it's gone
            response = await client.get(
                f"/api/v1/ui/workspaces/{test_workspace_with_owner.id}/environments/{environment_id}/target-roots"
            )
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_target_root_duplicate_name(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test that duplicate target root names in same environment are rejected."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        workspace_id=test_workspace_with_owner.id,
    )

    # Get the environment ID and create a target root
    from stardag_api.models import TargetRoot

    async with async_session_maker() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(Environment).where(
                Environment.workspace_id == test_workspace_with_owner.id
            )
        )
        environment = result.scalar_one()
        environment_id = environment.id

        target_root = TargetRoot(
            environment_id=environment_id,
            name="existing-name",
            uri_prefix="/existing/",
        )
        session.add(target_root)
        await session.commit()

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/ui/workspaces/{test_workspace_with_owner.id}/environments/{environment_id}/target-roots",
                json={
                    "name": "existing-name",  # Same name as existing
                    "uri_prefix": "/different/path/",
                },
            )

        assert response.status_code == 409
        data = response.json()
        assert "existing-name" in data["detail"]
    finally:
        app.dependency_overrides.clear()


# --- Personal Workspace tests ---


@pytest.mark.asyncio
async def test_cannot_invite_to_personal_workspace(async_engine):
    """Test that invites to personal workspaces are rejected."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    # Create a user with a personal workspace
    async with async_session_maker() as session:
        user = User(
            external_id="personal-ws-user",
            email="personalws@example.com",
            display_name="Personal WS User",
        )
        session.add(user)
        await session.flush()

        personal_workspace = Workspace(
            name="Personal Workspace",
            slug="personal-workspace",
            is_personal=True,
            created_by_id=user.id,
        )
        session.add(personal_workspace)
        await session.flush()

        membership = WorkspaceMember(
            workspace_id=personal_workspace.id,
            user_id=user.id,
            role=WorkspaceRole.OWNER,
        )
        session.add(membership)
        await session.commit()
        await session.refresh(user)
        await session.refresh(personal_workspace)

    mock_token = _create_mock_internal_token(
        user_id=user.id,
        workspace_id=personal_workspace.id,
    )

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/ui/workspaces/{personal_workspace.id}/invites",
                json={
                    "email": "invited@example.com",
                    "role": "member",
                },
            )

        assert response.status_code == 403
        data = response.json()
        assert "personal workspace" in data["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_workspace_with_underscores_in_slug(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test creating a workspace with underscores in slug."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_current_user_flexible():
        return test_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_flexible] = (
        override_get_current_user_flexible
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/ui/workspaces",
                json={
                    "name": "Underscore Workspace",
                    "slug": "my_workspace_123",
                    "description": "Workspace with underscores",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["slug"] == "my_workspace_123"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_workspace_with_initial_environment_and_target_root(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test creating a workspace with initial environment and target root."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_current_user_flexible():
        return test_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_flexible] = (
        override_get_current_user_flexible
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/ui/workspaces",
                json={
                    "name": "Full Setup Workspace",
                    "slug": "full-setup-workspace",
                    "description": "Workspace with all setup",
                    "initial_environment_name": "Production",
                    "initial_environment_slug": "production",
                    "initial_target_root_name": "default",
                    "initial_target_root_uri": "s3://my-bucket/stardag/",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["slug"] == "full-setup-workspace"
        workspace_id = data["id"]

        # Verify the environment and target root were created
        # Create a token for this new workspace
        mock_token = _create_mock_internal_token(
            user_id=test_user.id,
            workspace_id=workspace_id,
        )

        async def override_get_token():
            return mock_token

        app.dependency_overrides[get_token] = override_get_token

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Check environments
            response = await client.get(
                f"/api/v1/ui/workspaces/{workspace_id}/environments"
            )
            assert response.status_code == 200
            envs = response.json()
            # Should have the "production" environment and a personal environment
            env_slugs = [e["slug"] for e in envs]
            assert "production" in env_slugs

            # Find the production environment
            prod_env = next(e for e in envs if e["slug"] == "production")

            # Check target roots in production environment
            response = await client.get(
                f"/api/v1/ui/workspaces/{workspace_id}/environments/{prod_env['id']}/target-roots"
            )
            assert response.status_code == 200
            roots = response.json()
            assert len(roots) == 1
            assert roots[0]["name"] == "default"
            assert roots[0]["uri_prefix"] == "s3://my-bucket/stardag/"

    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_workspace_response_includes_is_personal(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test that workspace response includes is_personal field."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        workspace_id=test_workspace_with_owner.id,
    )

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                f"/api/v1/ui/workspaces/{test_workspace_with_owner.id}"
            )

        assert response.status_code == 200
        data = response.json()
        # The test workspace is not personal by default
        assert "is_personal" not in data or data.get("is_personal") is False
    finally:
        app.dependency_overrides.clear()


# --- Invite flow tests (bootstrap endpoints) ---


@pytest.mark.asyncio
async def test_fetch_pending_invites(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test fetching pending invites for a user via /me/invites endpoint.

    This is a bootstrap endpoint that uses OIDC ID token (not workspace-scoped token).
    """
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    # Create another user who will receive the invites
    async with async_session_maker() as session:
        invited_user = User(
            external_id="invited-user-pending",
            email="pendinguser@example.com",
            display_name="Pending User",
        )
        session.add(invited_user)
        await session.flush()

        # Give them a personal workspace (as would happen on first login)
        personal_workspace = Workspace(
            name="Pending User's Workspace",
            slug="pending-user-workspace",
            is_personal=True,
            created_by_id=invited_user.id,
        )
        session.add(personal_workspace)
        await session.flush()

        membership = WorkspaceMember(
            workspace_id=personal_workspace.id,
            user_id=invited_user.id,
            role=WorkspaceRole.OWNER,
        )
        session.add(membership)

        # Create two pending invites for this user
        from stardag_api.models import Invite, InviteStatus

        invite1 = Invite(
            workspace_id=test_workspace_with_owner.id,
            email=invited_user.email,
            role=WorkspaceRole.MEMBER,
            invited_by_id=test_user.id,
            status=InviteStatus.PENDING,
        )
        session.add(invite1)

        # Create another workspace with another invite
        other_workspace = Workspace(
            name="Other Workspace",
            slug="other-workspace-invites",
        )
        session.add(other_workspace)
        await session.flush()

        other_owner = User(
            external_id="other-owner",
            email="other-owner@example.com",
            display_name="Other Owner",
        )
        session.add(other_owner)
        await session.flush()

        other_membership = WorkspaceMember(
            workspace_id=other_workspace.id,
            user_id=other_owner.id,
            role=WorkspaceRole.OWNER,
        )
        session.add(other_membership)

        invite2 = Invite(
            workspace_id=other_workspace.id,
            email=invited_user.email,
            role=WorkspaceRole.ADMIN,
            invited_by_id=other_owner.id,
            status=InviteStatus.PENDING,
        )
        session.add(invite2)

        await session.commit()
        await session.refresh(invited_user)

    # Now fetch pending invites as the invited user (bootstrap endpoint)
    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_current_user_flexible():
        return invited_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_flexible] = (
        override_get_current_user_flexible
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/ui/me/invites")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

        # Verify the invite data structure
        workspace_names = {invite["workspace_name"] for invite in data}
        assert "Test Workspace" in workspace_names
        assert "Other Workspace" in workspace_names

        # Check that each invite has the expected fields
        for invite in data:
            assert "id" in invite
            assert "workspace_id" in invite
            assert "workspace_name" in invite
            assert "role" in invite
            assert "invited_by_email" in invite
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_decline_invite(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test declining an invite via /invites/{id}/decline endpoint.

    This is a bootstrap endpoint that uses OIDC ID token.
    """
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    # Create user who will decline the invite
    async with async_session_maker() as session:
        declining_user = User(
            external_id="declining-user",
            email="declining@example.com",
            display_name="Declining User",
        )
        session.add(declining_user)
        await session.flush()

        # Give them a personal workspace
        personal_workspace = Workspace(
            name="Declining User's Workspace",
            slug="declining-user-workspace",
            is_personal=True,
            created_by_id=declining_user.id,
        )
        session.add(personal_workspace)
        await session.flush()

        membership = WorkspaceMember(
            workspace_id=personal_workspace.id,
            user_id=declining_user.id,
            role=WorkspaceRole.OWNER,
        )
        session.add(membership)

        from stardag_api.models import Invite, InviteStatus

        invite = Invite(
            workspace_id=test_workspace_with_owner.id,
            email=declining_user.email,
            role=WorkspaceRole.MEMBER,
            invited_by_id=test_user.id,
            status=InviteStatus.PENDING,
        )
        session.add(invite)
        await session.commit()
        await session.refresh(invite)
        await session.refresh(declining_user)
        invite_id = invite.id

    # Decline the invite
    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_current_user_flexible():
        return declining_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_flexible] = (
        override_get_current_user_flexible
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/ui/workspaces/invites/{invite_id}/decline"
            )

        assert response.status_code == 204

        # Verify there are no more pending invites
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/ui/me/invites")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_accept_invite_wrong_user(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test that a user cannot accept an invite meant for a different email."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    # Create an invite for one email
    async with async_session_maker() as session:
        from stardag_api.models import Invite, InviteStatus

        invite = Invite(
            workspace_id=test_workspace_with_owner.id,
            email="specific@example.com",  # Invite is for this email
            role=WorkspaceRole.MEMBER,
            invited_by_id=test_user.id,
            status=InviteStatus.PENDING,
        )
        session.add(invite)
        await session.commit()
        await session.refresh(invite)
        invite_id = invite.id

    # Create a different user who will try to accept
    async with async_session_maker() as session:
        wrong_user = User(
            external_id="wrong-user",
            email="different@example.com",  # Different email
            display_name="Wrong User",
        )
        session.add(wrong_user)
        await session.commit()
        await session.refresh(wrong_user)

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_current_user_flexible():
        return wrong_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_flexible] = (
        override_get_current_user_flexible
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/ui/workspaces/invites/{invite_id}/accept"
            )

        # Should be not found - invite is for a different email
        # (The API returns 404 rather than 403 to not leak invite existence)
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_fetch_user_profile_with_workspaces(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test fetching user profile via /me endpoint.

    This is a bootstrap endpoint that returns user info and workspace list.
    """
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_current_user_flexible():
        return test_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_flexible] = (
        override_get_current_user_flexible
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/ui/me")

        assert response.status_code == 200
        data = response.json()

        # Check user info
        assert "user" in data
        assert data["user"]["email"] == test_user.email
        assert data["user"]["id"] == str(test_user.id)

        # Check workspaces list
        assert "workspaces" in data
        assert len(data["workspaces"]) == 1
        assert data["workspaces"][0]["name"] == "Test Workspace"
        assert data["workspaces"][0]["role"] == "owner"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_invite_flow_full_cycle(
    async_engine, test_user: User, test_workspace_with_owner: Workspace
):
    """Test the complete invite flow: create invite, fetch pending, accept.

    This tests the full cycle that a user goes through when being invited.
    """
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    # Step 1: Owner creates an invite (workspace-scoped endpoint)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        workspace_id=test_workspace_with_owner.id,
    )

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/ui/workspaces/{test_workspace_with_owner.id}/invites",
                json={
                    "email": "newmember@example.com",
                    "role": "member",
                },
            )
        assert response.status_code == 201
        invite_data = response.json()
        invite_id = invite_data["id"]
    finally:
        app.dependency_overrides.clear()

    # Step 2: New user signs up and gets a personal workspace
    async with async_session_maker() as session:
        new_user = User(
            external_id="new-member-user",
            email="newmember@example.com",  # Matches the invite
            display_name="New Member",
        )
        session.add(new_user)
        await session.flush()

        # Auto-created personal workspace
        personal_workspace = Workspace(
            name="New Member's Workspace",
            slug="new-member-workspace",
            is_personal=True,
            created_by_id=new_user.id,
        )
        session.add(personal_workspace)
        await session.flush()

        personal_membership = WorkspaceMember(
            workspace_id=personal_workspace.id,
            user_id=new_user.id,
            role=WorkspaceRole.OWNER,
        )
        session.add(personal_membership)
        await session.commit()
        await session.refresh(new_user)

    # Step 3: New user fetches their pending invites (bootstrap endpoint)
    async def override_get_current_user_flexible():
        return new_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_flexible] = (
        override_get_current_user_flexible
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/ui/me/invites")

        assert response.status_code == 200
        invites = response.json()
        assert len(invites) == 1
        assert invites[0]["workspace_name"] == "Test Workspace"
        assert invites[0]["id"] == invite_id
    finally:
        app.dependency_overrides.clear()

    # Step 4: New user accepts the invite (bootstrap endpoint)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_flexible] = (
        override_get_current_user_flexible
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/ui/workspaces/invites/{invite_id}/accept"
            )

        assert response.status_code == 200
        workspace_data = response.json()
        assert workspace_data["id"] == str(test_workspace_with_owner.id)
    finally:
        app.dependency_overrides.clear()

    # Step 5: Verify user now has access to both workspaces via /me
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_flexible] = (
        override_get_current_user_flexible
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/ui/me")

        assert response.status_code == 200
        profile = response.json()
        assert len(profile["workspaces"]) == 2

        workspace_names = {ws["name"] for ws in profile["workspaces"]}
        assert "New Member's Workspace" in workspace_names
        assert "Test Workspace" in workspace_names

        # Check roles
        for ws in profile["workspaces"]:
            if ws["name"] == "New Member's Workspace":
                assert ws["role"] == "owner"
            elif ws["name"] == "Test Workspace":
                assert ws["role"] == "member"
    finally:
        app.dependency_overrides.clear()

    # Step 6: Verify no more pending invites
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_flexible] = (
        override_get_current_user_flexible
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/ui/me/invites")

        assert response.status_code == 200
        invites = response.json()
        assert len(invites) == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_personal_workspace_is_personal_flag_in_profile(async_engine):
    """Test that /me returns is_personal=true for personal workspaces.

    This is critical for the onboarding modal to display correctly for new users.
    """
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    # Create a user with a personal workspace (simulating new user signup)
    async with async_session_maker() as session:
        user = User(
            external_id="new-signup-user",
            email="newsignup@example.com",
            display_name="New Signup User",
        )
        session.add(user)
        await session.flush()

        # Create personal workspace (as would happen in auto-create)
        personal_workspace = Workspace(
            name="New Signup User's Workspace",
            slug="new-signup-user-workspace",
            is_personal=True,
            created_by_id=user.id,
        )
        session.add(personal_workspace)
        await session.flush()

        membership = WorkspaceMember(
            workspace_id=personal_workspace.id,
            user_id=user.id,
            role=WorkspaceRole.OWNER,
        )
        session.add(membership)

        # Create local environment
        environment = Environment(
            workspace_id=personal_workspace.id,
            name="Local",
            slug="local",
        )
        session.add(environment)

        await session.commit()
        await session.refresh(user)

    # Fetch profile as the new user
    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_current_user_flexible():
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_flexible] = (
        override_get_current_user_flexible
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/ui/me")

        assert response.status_code == 200
        data = response.json()

        # Verify user info
        assert data["user"]["email"] == "newsignup@example.com"

        # Verify workspace info - MUST have is_personal=True
        assert len(data["workspaces"]) == 1
        workspace = data["workspaces"][0]
        assert workspace["is_personal"] is True
        assert workspace["role"] == "owner"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_new_user_with_personal_workspace_and_pending_invite(async_engine):
    """Test the full onboarding flow: new user with personal workspace and pending invite.

    This tests the scenario where:
    1. An existing user invites someone to their workspace
    2. The invitee signs up (gets auto-created personal workspace)
    3. The invitee should see both their personal workspace AND the pending invite

    This is the critical scenario for the onboarding modal.
    """
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    # Step 1: Create an existing workspace owner who will invite someone
    async with async_session_maker() as session:
        owner = User(
            external_id="workspace-owner",
            email="owner@example.com",
            display_name="Workspace Owner",
        )
        session.add(owner)
        await session.flush()

        team_workspace = Workspace(
            name="Team Workspace",
            slug="team-workspace",
            is_personal=False,
        )
        session.add(team_workspace)
        await session.flush()

        owner_membership = WorkspaceMember(
            workspace_id=team_workspace.id,
            user_id=owner.id,
            role=WorkspaceRole.OWNER,
        )
        session.add(owner_membership)

        # Create an invite for a new user
        from stardag_api.models import Invite, InviteStatus

        invite = Invite(
            workspace_id=team_workspace.id,
            email="newbie@example.com",
            role=WorkspaceRole.MEMBER,
            invited_by_id=owner.id,
            status=InviteStatus.PENDING,
        )
        session.add(invite)
        await session.commit()
        await session.refresh(invite)
        invite_id = invite.id

    # Step 2: New user signs up (simulated by creating user + personal workspace)
    async with async_session_maker() as session:
        newbie = User(
            external_id="newbie-user",
            email="newbie@example.com",
            display_name="New User",
        )
        session.add(newbie)
        await session.flush()

        personal_workspace = Workspace(
            name="New User's Workspace",
            slug="new-user-workspace",
            is_personal=True,
            created_by_id=newbie.id,
        )
        session.add(personal_workspace)
        await session.flush()

        newbie_membership = WorkspaceMember(
            workspace_id=personal_workspace.id,
            user_id=newbie.id,
            role=WorkspaceRole.OWNER,
        )
        session.add(newbie_membership)

        # Create local environment
        environment = Environment(
            workspace_id=personal_workspace.id,
            name="Local",
            slug="local",
        )
        session.add(environment)

        await session.commit()
        await session.refresh(newbie)

    # Step 3: Verify the new user sees their personal workspace with is_personal=True
    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_current_user_flexible():
        return newbie

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_flexible] = (
        override_get_current_user_flexible
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Check profile - should have 1 personal workspace
            response = await client.get("/api/v1/ui/me")
            assert response.status_code == 200
            profile = response.json()

            assert len(profile["workspaces"]) == 1
            assert profile["workspaces"][0]["is_personal"] is True
            assert profile["workspaces"][0]["role"] == "owner"

            # Check pending invites - should have 1 invite
            response = await client.get("/api/v1/ui/me/invites")
            assert response.status_code == 200
            invites = response.json()

            assert len(invites) == 1
            assert invites[0]["workspace_name"] == "Team Workspace"
            assert invites[0]["id"] == str(invite_id)

            # Accept the invite
            response = await client.post(
                f"/api/v1/ui/workspaces/invites/{invite_id}/accept"
            )
            assert response.status_code == 200

            # Verify user now has 2 workspaces
            response = await client.get("/api/v1/ui/me")
            assert response.status_code == 200
            profile = response.json()

            assert len(profile["workspaces"]) == 2
            workspace_names = {ws["name"] for ws in profile["workspaces"]}
            assert "New User's Workspace" in workspace_names
            assert "Team Workspace" in workspace_names

            # Verify is_personal flags are correct
            for ws in profile["workspaces"]:
                if ws["name"] == "New User's Workspace":
                    assert ws["is_personal"] is True
                elif ws["name"] == "Team Workspace":
                    assert ws["is_personal"] is False
    finally:
        app.dependency_overrides.clear()
