"""Tests for organization management routes."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from stardag_api.auth.dependencies import get_or_create_user_from_keycloak, get_token
from stardag_api.auth.tokens import InternalTokenPayload
from stardag_api.db import get_db
from stardag_api.main import app
from stardag_api.models import Organization, OrganizationMember, User, Workspace
from stardag_api.models.enums import OrganizationRole


def _create_mock_internal_token(
    user_id: str,
    org_id: str,
) -> InternalTokenPayload:
    """Create a mock internal token payload for testing.

    Internal tokens use the internal user ID (not external_id from OIDC).
    """
    return InternalTokenPayload(
        sub=user_id,
        org_id=org_id,
        iss="stardag-api",
        aud="stardag",
        exp=9999999999,
        iat=1234567800,
    )


@pytest.fixture
async def test_user_with_org(async_session: AsyncSession) -> tuple[User, Organization]:
    """Create a test user with their own organization.

    Returns (user, organization) tuple. The user is owner of the organization.
    """
    # Create user
    user = User(
        external_id="test-org-user",
        email="orgtest@example.com",
        display_name="Org Test User",
    )
    async_session.add(user)
    await async_session.flush()  # Get user.id

    # Create organization
    org = Organization(
        name="Test Organization",
        slug="test-org",
        description="A test organization",
    )
    async_session.add(org)
    await async_session.flush()  # Get org.id

    # Create membership
    membership = OrganizationMember(
        organization_id=org.id,
        user_id=user.id,
        role=OrganizationRole.OWNER,
    )
    async_session.add(membership)

    # Create default workspace
    workspace = Workspace(
        organization_id=org.id,
        name="Default",
        slug="default",
    )
    async_session.add(workspace)

    await async_session.commit()
    await async_session.refresh(user)
    await async_session.refresh(org)

    return user, org


@pytest.fixture
async def test_user(test_user_with_org: tuple[User, Organization]) -> User:
    """Get the test user (for backwards compatibility with tests)."""
    return test_user_with_org[0]


@pytest.fixture
async def test_org_with_owner(
    test_user_with_org: tuple[User, Organization],
) -> Organization:
    """Get the test organization (for backwards compatibility with tests)."""
    return test_user_with_org[1]


@pytest.fixture
def mock_token_for_user(test_user_with_org: tuple[User, Organization]):
    """Create a mock internal token for the test user."""
    user, org = test_user_with_org
    return _create_mock_internal_token(
        user_id=user.id,
        org_id=org.id,
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


# --- Organization CRUD tests ---


@pytest.mark.asyncio
async def test_create_organization(
    async_engine, test_user: User, test_org_with_owner: Organization
):
    """Test creating a new organization.

    Note: Creating an organization is a bootstrap endpoint that accepts Keycloak tokens
    (not internal tokens).
    """
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_or_create_user_from_keycloak():
        return test_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_or_create_user_from_keycloak] = (
        override_get_or_create_user_from_keycloak
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/ui/organizations",
                json={
                    "name": "New Org",
                    "slug": "new-org",
                    "description": "A new organization",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "New Org"
        assert data["slug"] == "new-org"
        assert data["description"] == "A new organization"
        assert "id" in data
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_organization_duplicate_slug(
    async_engine, test_user: User, test_org_with_owner: Organization
):
    """Test that duplicate slugs are rejected.

    Note: Creating an organization is a bootstrap endpoint that accepts Keycloak tokens
    (not internal tokens).
    """
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_or_create_user_from_keycloak():
        return test_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_or_create_user_from_keycloak] = (
        override_get_or_create_user_from_keycloak
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/ui/organizations",
                json={
                    "name": "Another Org",
                    "slug": "test-org",  # Same as test_org_with_owner
                },
            )

        assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_organization(
    async_engine, test_user: User, test_org_with_owner: Organization
):
    """Test getting organization details."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        org_id=test_org_with_owner.id,
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
                f"/api/v1/ui/organizations/{test_org_with_owner.id}"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Test Organization"
        assert data["member_count"] == 1
        assert data["workspace_count"] == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_organization_not_member(
    async_engine, test_org_with_owner: Organization
):
    """Test that users can't access organizations they're not a member of."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    # Create a different user with their own organization
    async with async_session_maker() as session:
        other_user = User(
            external_id="other-user",
            email="other@example.com",
            display_name="Other User",
        )
        session.add(other_user)
        await session.flush()

        other_org = Organization(
            name="Other Org",
            slug="other-org",
        )
        session.add(other_org)
        await session.flush()

        membership = OrganizationMember(
            organization_id=other_org.id,
            user_id=other_user.id,
            role=OrganizationRole.OWNER,
        )
        session.add(membership)
        await session.commit()
        await session.refresh(other_user)
        await session.refresh(other_org)

    # Token for other_user scoped to their own org
    mock_token = _create_mock_internal_token(
        user_id=other_user.id,
        org_id=other_org.id,
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
                f"/api/v1/ui/organizations/{test_org_with_owner.id}"
            )

        assert response.status_code == 404  # Not found (rather than 403)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_update_organization(
    async_engine, test_user: User, test_org_with_owner: Organization
):
    """Test updating organization."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        org_id=test_org_with_owner.id,
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
                f"/api/v1/ui/organizations/{test_org_with_owner.id}",
                json={"name": "Updated Name"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Name"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_organization(
    async_engine, test_user: User, test_org_with_owner: Organization
):
    """Test deleting organization (owner only)."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        org_id=test_org_with_owner.id,
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
                f"/api/v1/ui/organizations/{test_org_with_owner.id}"
            )

        assert response.status_code == 204
    finally:
        app.dependency_overrides.clear()


# --- Workspace tests ---


@pytest.mark.asyncio
async def test_list_workspaces(
    async_engine, test_user: User, test_org_with_owner: Organization
):
    """Test listing workspaces."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        org_id=test_org_with_owner.id,
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
                f"/api/v1/ui/organizations/{test_org_with_owner.id}/workspaces"
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["slug"] == "default"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_workspace(
    async_engine, test_user: User, test_org_with_owner: Organization
):
    """Test creating a workspace."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        org_id=test_org_with_owner.id,
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
                f"/api/v1/ui/organizations/{test_org_with_owner.id}/workspaces",
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
    finally:
        app.dependency_overrides.clear()


# --- Member tests ---


@pytest.mark.asyncio
async def test_list_members(
    async_engine, test_user: User, test_org_with_owner: Organization
):
    """Test listing organization members."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        org_id=test_org_with_owner.id,
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
                f"/api/v1/ui/organizations/{test_org_with_owner.id}/members"
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
    async_engine, test_user: User, test_org_with_owner: Organization
):
    """Test creating an invite."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        org_id=test_org_with_owner.id,
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
                f"/api/v1/ui/organizations/{test_org_with_owner.id}/invites",
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
    async_engine, test_user: User, test_org_with_owner: Organization
):
    """Test accepting an invite."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    # Create another user with their own org, and an invite for them
    async with async_session_maker() as session:
        invited_user = User(
            external_id="invited-user",
            email="invited@example.com",
            display_name="Invited User",
        )
        session.add(invited_user)
        await session.flush()

        # Create a personal org for the invited user (as would happen on first login)
        invited_user_org = Organization(
            name="Invited User's Org",
            slug="invited-user-org",
        )
        session.add(invited_user_org)
        await session.flush()

        invited_user_membership = OrganizationMember(
            organization_id=invited_user_org.id,
            user_id=invited_user.id,
            role=OrganizationRole.OWNER,
        )
        session.add(invited_user_membership)
        await session.commit()
        await session.refresh(invited_user)
        await session.refresh(invited_user_org)

        from stardag_api.models import Invite, InviteStatus

        invite = Invite(
            organization_id=test_org_with_owner.id,
            email=invited_user.email,
            role=OrganizationRole.MEMBER,
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

    async def override_get_or_create_user_from_keycloak():
        return invited_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_or_create_user_from_keycloak] = (
        override_get_or_create_user_from_keycloak
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/ui/organizations/invites/{invite_id}/accept"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == test_org_with_owner.id
    finally:
        app.dependency_overrides.clear()


# --- Target Root tests ---


@pytest.mark.asyncio
async def test_create_target_root(
    async_engine, test_user: User, test_org_with_owner: Organization
):
    """Test creating a target root in a workspace."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        org_id=test_org_with_owner.id,
    )

    # Get the workspace ID
    async with async_session_maker() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(Workspace).where(Workspace.organization_id == test_org_with_owner.id)
        )
        workspace = result.scalar_one()
        workspace_id = workspace.id

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
                f"/api/v1/ui/organizations/{test_org_with_owner.id}/workspaces/{workspace_id}/target-roots",
                json={
                    "name": "default",
                    "uri_prefix": "s3://my-bucket/stardag/",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "default"
        assert data["uri_prefix"] == "s3://my-bucket/stardag/"
        assert data["workspace_id"] == workspace_id
        assert "id" in data
        assert "created_at" in data
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_target_roots(
    async_engine, test_user: User, test_org_with_owner: Organization
):
    """Test listing target roots in a workspace."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        org_id=test_org_with_owner.id,
    )

    # Get the workspace ID and create a target root
    from stardag_api.models import TargetRoot

    async with async_session_maker() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(Workspace).where(Workspace.organization_id == test_org_with_owner.id)
        )
        workspace = result.scalar_one()
        workspace_id = workspace.id

        target_root = TargetRoot(
            workspace_id=workspace_id,
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
                f"/api/v1/ui/organizations/{test_org_with_owner.id}/workspaces/{workspace_id}/target-roots"
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
    async_engine, test_user: User, test_org_with_owner: Organization
):
    """Test updating a target root."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        org_id=test_org_with_owner.id,
    )

    # Get the workspace ID and create a target root
    from stardag_api.models import TargetRoot

    async with async_session_maker() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(Workspace).where(Workspace.organization_id == test_org_with_owner.id)
        )
        workspace = result.scalar_one()
        workspace_id = workspace.id

        target_root = TargetRoot(
            workspace_id=workspace_id,
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
                f"/api/v1/ui/organizations/{test_org_with_owner.id}/workspaces/{workspace_id}/target-roots/{root_id}",
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
    async_engine, test_user: User, test_org_with_owner: Organization
):
    """Test deleting a target root."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        org_id=test_org_with_owner.id,
    )

    # Get the workspace ID and create a target root
    from stardag_api.models import TargetRoot

    async with async_session_maker() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(Workspace).where(Workspace.organization_id == test_org_with_owner.id)
        )
        workspace = result.scalar_one()
        workspace_id = workspace.id

        target_root = TargetRoot(
            workspace_id=workspace_id,
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
                f"/api/v1/ui/organizations/{test_org_with_owner.id}/workspaces/{workspace_id}/target-roots/{root_id}"
            )
            assert response.status_code == 204

            # Verify it's gone
            response = await client.get(
                f"/api/v1/ui/organizations/{test_org_with_owner.id}/workspaces/{workspace_id}/target-roots"
            )
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_target_root_duplicate_name(
    async_engine, test_user: User, test_org_with_owner: Organization
):
    """Test that duplicate target root names in same workspace are rejected."""
    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    mock_token = _create_mock_internal_token(
        user_id=test_user.id,
        org_id=test_org_with_owner.id,
    )

    # Get the workspace ID and create a target root
    from stardag_api.models import TargetRoot

    async with async_session_maker() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(Workspace).where(Workspace.organization_id == test_org_with_owner.id)
        )
        workspace = result.scalar_one()
        workspace_id = workspace.id

        target_root = TargetRoot(
            workspace_id=workspace_id,
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
                f"/api/v1/ui/organizations/{test_org_with_owner.id}/workspaces/{workspace_id}/target-roots",
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
