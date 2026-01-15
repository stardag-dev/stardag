"""Tests for workspace management routes."""

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
    user_id: str,
    workspace_id: str,
) -> InternalTokenPayload:
    """Create a mock internal token payload for testing.

    Internal tokens use the internal user ID (not external_id from OIDC).
    """
    return InternalTokenPayload(
        sub=user_id,
        workspace_id=workspace_id,
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
        assert data["id"] == test_workspace_with_owner.id
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
        assert data["environment_id"] == environment_id
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
