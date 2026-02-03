"""Tests for authentication flow."""

import time
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from stardag_api.auth.dependencies import get_current_user_flexible
from stardag_api.auth.jwt import AuthenticationError, JWTValidator, TokenPayload
from stardag_api.auth.tokens import (
    InternalTokenManager,
    InternalTokenPayload,
    TokenInvalidError,
)
from stardag_api.db import get_db
from stardag_api.main import app
from stardag_api.models import Workspace, WorkspaceMember, User
from stardag_api.models.enums import WorkspaceRole


# --- Keycloak TokenPayload tests (for /auth/exchange) ---


def test_token_payload_from_dict():
    """Test TokenPayload.from_dict with minimal claims."""
    payload = {
        "sub": "user-123",
        "iss": "https://example.com",
        "exp": 1234567890,
    }
    token = TokenPayload.from_dict(payload)

    assert token.sub == "user-123"
    assert token.iss == "https://example.com"
    assert token.exp == 1234567890
    assert token.email is None
    assert token.name is None


def test_token_payload_from_dict_missing_sub():
    """Test TokenPayload.from_dict raises error for missing sub claim."""
    payload = {
        "iss": "https://example.com",
        "exp": 1234567890,
    }
    with pytest.raises(AuthenticationError) as exc_info:
        TokenPayload.from_dict(payload)
    assert "sub" in str(exc_info.value).lower()


def test_token_payload_from_dict_missing_iss():
    """Test TokenPayload.from_dict raises error for missing iss claim."""
    payload = {
        "sub": "user-123",
        "exp": 1234567890,
    }
    with pytest.raises(AuthenticationError) as exc_info:
        TokenPayload.from_dict(payload)
    assert "iss" in str(exc_info.value).lower()


def test_token_payload_from_dict_missing_exp():
    """Test TokenPayload.from_dict raises error for missing exp claim."""
    payload = {
        "sub": "user-123",
        "iss": "https://example.com",
    }
    with pytest.raises(AuthenticationError) as exc_info:
        TokenPayload.from_dict(payload)
    assert "exp" in str(exc_info.value).lower()


def test_token_payload_from_dict_full_claims():
    """Test TokenPayload.from_dict with all claims."""
    payload = {
        "sub": "user-123",
        "email": "test@example.com",
        "email_verified": True,
        "name": "Test User",
        "given_name": "Test",
        "family_name": "User",
        "preferred_username": "testuser",
        "iss": "https://example.com",
        "aud": "stardag-ui",
        "exp": 1234567890,
        "iat": 1234567800,
    }
    token = TokenPayload.from_dict(payload)

    assert token.sub == "user-123"
    assert token.email == "test@example.com"
    assert token.email_verified is True
    assert token.name == "Test User"
    assert token.given_name == "Test"
    assert token.family_name == "User"
    assert token.preferred_username == "testuser"
    assert token.iss == "https://example.com"
    assert token.aud == "stardag-ui"
    assert token.exp == 1234567890
    assert token.iat == 1234567800


def test_token_payload_display_name_from_name():
    """Test display_name returns name when available."""
    token = TokenPayload(
        sub="user-123",
        email="test@example.com",
        email_verified=True,
        name="Full Name",
        given_name="Test",
        family_name="User",
        preferred_username="testuser",
        iss="https://example.com",
        aud="stardag-ui",
        exp=1234567890,
        iat=1234567800,
        raw={},
    )
    assert token.display_name == "Full Name"


def test_token_payload_display_name_from_given_family():
    """Test display_name combines given/family names when name not available."""
    token = TokenPayload(
        sub="user-123",
        email="test@example.com",
        email_verified=True,
        name=None,
        given_name="Test",
        family_name="User",
        preferred_username="testuser",
        iss="https://example.com",
        aud="stardag-ui",
        exp=1234567890,
        iat=1234567800,
        raw={},
    )
    assert token.display_name == "Test User"


def test_token_payload_display_name_from_username():
    """Test display_name falls back to preferred_username."""
    token = TokenPayload(
        sub="user-123",
        email="test@example.com",
        email_verified=True,
        name=None,
        given_name=None,
        family_name=None,
        preferred_username="testuser",
        iss="https://example.com",
        aud="stardag-ui",
        exp=1234567890,
        iat=1234567800,
        raw={},
    )
    assert token.display_name == "testuser"


def test_token_payload_display_name_from_email():
    """Test display_name falls back to email prefix."""
    token = TokenPayload(
        sub="user-123",
        email="test@example.com",
        email_verified=True,
        name=None,
        given_name=None,
        family_name=None,
        preferred_username=None,
        iss="https://example.com",
        aud="stardag-ui",
        exp=1234567890,
        iat=1234567800,
        raw={},
    )
    assert token.display_name == "test"


def test_token_payload_display_name_fallback_to_sub():
    """Test display_name falls back to sub when nothing else available."""
    token = TokenPayload(
        sub="user-123",
        email=None,
        email_verified=False,
        name=None,
        given_name=None,
        family_name=None,
        preferred_username=None,
        iss="https://example.com",
        aud="stardag-ui",
        exp=1234567890,
        iat=1234567800,
        raw={},
    )
    assert token.display_name == "user-123"


# --- Keycloak JWTValidator tests ---


@pytest.mark.asyncio
async def test_jwt_validator_caches_jwks():
    """Test that JWTValidator caches JWKS."""
    validator = JWTValidator(
        jwks_url="https://example.com/.well-known/jwks.json",
        allowed_issuers=["https://example.com"],
        audiences=["stardag-ui"],
        cache_ttl=300,
    )

    mock_jwks = {"keys": [{"kid": "test-key", "kty": "RSA", "n": "abc", "e": "AQAB"}]}

    with patch.object(validator, "_fetch_jwks", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_jwks

        # First call should fetch
        result1 = await validator._get_jwks()
        assert result1 == mock_jwks
        assert mock_fetch.call_count == 1

        # Second call should use cache
        result2 = await validator._get_jwks()
        assert result2 == mock_jwks
        assert mock_fetch.call_count == 1  # Still 1


@pytest.mark.asyncio
async def test_jwt_validator_force_refresh():
    """Test that force_refresh bypasses cache."""
    validator = JWTValidator(
        jwks_url="https://example.com/.well-known/jwks.json",
        allowed_issuers=["https://example.com"],
        audiences=["stardag-ui"],
        cache_ttl=300,
    )

    mock_jwks = {"keys": [{"kid": "test-key", "kty": "RSA", "n": "abc", "e": "AQAB"}]}

    with patch.object(validator, "_fetch_jwks", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_jwks

        await validator._get_jwks()
        assert mock_fetch.call_count == 1

        # Force refresh should fetch again
        await validator._get_jwks(force_refresh=True)
        assert mock_fetch.call_count == 2


@pytest.mark.asyncio
async def test_jwt_validator_cache_expiry():
    """Test that JWKS cache expires."""
    validator = JWTValidator(
        jwks_url="https://example.com/.well-known/jwks.json",
        allowed_issuers=["https://example.com"],
        audiences=["stardag-ui"],
        cache_ttl=1,  # 1 second TTL
    )

    mock_jwks = {"keys": [{"kid": "test-key", "kty": "RSA", "n": "abc", "e": "AQAB"}]}

    with patch.object(validator, "_fetch_jwks", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_jwks

        await validator._get_jwks()
        assert mock_fetch.call_count == 1

        # Wait for cache to expire
        time.sleep(1.1)

        await validator._get_jwks()
        assert mock_fetch.call_count == 2


@pytest.mark.asyncio
async def test_jwt_validator_missing_kid():
    """Test that tokens without kid are rejected."""
    validator = JWTValidator(
        jwks_url="https://example.com/.well-known/jwks.json",
        allowed_issuers=["https://example.com"],
        audiences=["stardag-ui"],
    )

    # Token without kid in header (base64 encoded {"alg": "RS256", "typ": "JWT"})
    token_no_kid = (
        "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig"
    )

    with pytest.raises(AuthenticationError) as exc_info:
        await validator.validate_token(token_no_kid)

    assert "missing key ID" in str(exc_info.value)


# --- Internal Token Manager tests ---


def test_internal_token_manager_creates_valid_token():
    """Test that InternalTokenManager creates valid tokens."""
    manager = InternalTokenManager(
        issuer="test-issuer",
        audience="test-audience",
        access_token_ttl_minutes=10,
    )

    token = manager.create_access_token(
        user_id="user-123", workspace_id="workspace-456"
    )
    assert token is not None

    # Validate the token
    payload = manager.validate_token(token)
    assert payload.sub == "user-123"
    assert payload.workspace_id == "workspace-456"
    assert payload.iss == "test-issuer"
    assert payload.aud == "test-audience"


def test_internal_token_manager_validates_own_tokens():
    """Test that tokens can be validated by the same manager."""
    manager = InternalTokenManager()

    token = manager.create_access_token(user_id="user-1", workspace_id="workspace-1")
    payload = manager.validate_token(token)

    assert payload.sub == "user-1"
    assert payload.workspace_id == "workspace-1"


def test_internal_token_manager_rejects_invalid_token():
    """Test that invalid tokens are rejected."""
    manager = InternalTokenManager()

    with pytest.raises(TokenInvalidError):
        manager.validate_token("invalid-token")


def test_internal_token_manager_rejects_wrong_issuer():
    """Test that tokens from wrong issuer are rejected."""
    manager1 = InternalTokenManager(issuer="issuer-1")
    manager2 = InternalTokenManager(issuer="issuer-2")

    token = manager1.create_access_token(user_id="user-1", workspace_id="workspace-1")

    with pytest.raises(TokenInvalidError):
        manager2.validate_token(token)


def test_internal_token_manager_generates_jwks():
    """Test that JWKS is generated correctly."""
    manager = InternalTokenManager()
    jwks = manager.get_jwks()

    assert "keys" in jwks
    assert len(jwks["keys"]) == 1

    key = jwks["keys"][0]
    assert key["kty"] == "RSA"
    assert key["use"] == "sig"
    assert key["alg"] == "RS256"
    assert "kid" in key
    assert "n" in key
    assert "e" in key


def test_internal_token_payload_from_dict():
    """Test InternalTokenPayload.from_dict."""
    payload = {
        "sub": "user-123",
        "workspace_id": "workspace-456",
        "iss": "stardag-api",
        "aud": "stardag",
        "exp": 9999999999,
        "iat": 1234567890,
    }
    token = InternalTokenPayload.from_dict(payload)

    assert token.sub == "user-123"
    assert token.workspace_id == "workspace-456"
    assert token.iss == "stardag-api"
    assert token.aud == "stardag"


def test_internal_token_payload_requires_workspace_id():
    """Test that workspace_id is required for internal tokens."""
    payload = {
        "sub": "user-123",
        # Missing workspace_id
        "iss": "stardag-api",
        "aud": "stardag",
        "exp": 9999999999,
        "iat": 1234567890,
    }
    with pytest.raises(TokenInvalidError) as exc_info:
        InternalTokenPayload.from_dict(payload)
    assert "workspace_id" in str(exc_info.value)


# --- Helper to create mock internal tokens ---


def _create_internal_token(
    user_id: str,
    workspace_id: str,
    manager: InternalTokenManager | None = None,
) -> str:
    """Create a valid internal token for testing."""
    if manager is None:
        manager = InternalTokenManager()
    return manager.create_access_token(user_id=user_id, workspace_id=workspace_id)


def _create_mock_internal_token_payload(
    user_id: str = "test-user-id",
    workspace_id: str = "test-workspace-id",
) -> InternalTokenPayload:
    """Create a mock internal token payload for dependency overrides."""
    return InternalTokenPayload(
        sub=user_id,
        workspace_id=workspace_id,
        iss="stardag-api",
        aud="stardag",
        exp=9999999999,
        iat=1234567890,
    )


# --- /api/v1/ui/me endpoint tests ---


@pytest.mark.asyncio
async def test_me_endpoint_requires_auth(unauthenticated_client: AsyncClient):
    """Test that /api/v1/ui/me requires authentication."""
    response = await unauthenticated_client.get("/api/v1/ui/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_me_endpoint_returns_user_profile(async_engine):
    """Test that /api/v1/ui/me returns user profile.

    Note: /me is a bootstrap endpoint that accepts Keycloak tokens (not internal tokens).
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    # Create a user first
    async with async_session_maker() as session:
        user = User(
            external_id="test-user-sub",
            email="test@example.com",
            display_name="Test User",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        mock_user = user

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_current_user_flexible():
        return mock_user

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
        assert "user" in data
        assert "workspaces" in data
        assert data["user"]["external_id"] == "test-user-sub"
        assert data["user"]["email"] == "test@example.com"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_me_endpoint_returns_workspaces(async_engine):
    """Test that /api/v1/ui/me returns user's workspaces.

    Note: /me is a bootstrap endpoint that accepts Keycloak tokens (not internal tokens).
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    # Create a user with workspace membership
    async with async_session_maker() as session:
        user = User(
            external_id="user-with-workspace",
            email="workspaceuser@example.com",
            display_name="Workspace User",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        mock_user = user

        workspace = Workspace(
            name="Test Workspace",
            slug="test-workspace",
        )
        session.add(workspace)
        await session.commit()
        await session.refresh(workspace)

        membership = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.ADMIN,
        )
        session.add(membership)
        await session.commit()

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_current_user_flexible():
        return mock_user

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
        assert len(data["workspaces"]) == 1
        assert data["workspaces"][0]["name"] == "Test Workspace"
        assert data["workspaces"][0]["slug"] == "test-workspace"
        assert data["workspaces"][0]["role"] == "admin"
    finally:
        app.dependency_overrides.clear()


# --- Personal workspace creation tests ---


@pytest.mark.asyncio
async def test_create_personal_workspace_generates_unique_slug(async_engine):
    """Test that personal workspace creation generates unique slug with random suffix."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from stardag_api.auth.dependencies import create_personal_workspace_for_user

    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    async with async_session_maker() as session:
        # Create first user with email that will generate slug "testuser"
        user1 = User(
            external_id="user-1-ext",
            email="testuser@example.com",
            display_name="Test User",
        )
        session.add(user1)
        await session.commit()
        await session.refresh(user1)

        # Create personal workspace for user1
        workspace1 = await create_personal_workspace_for_user(session, user1)
        await session.commit()

        assert workspace1.slug == "testuser"  # First one gets the base slug
        assert workspace1.is_personal is True
        assert workspace1.name == "Test User's Workspace"

        # Create second user with same email prefix
        user2 = User(
            external_id="user-2-ext",
            email="testuser@different.com",
            display_name="Test User 2",
        )
        session.add(user2)
        await session.commit()
        await session.refresh(user2)

        # Create personal workspace for user2 - should get slug with random suffix
        workspace2 = await create_personal_workspace_for_user(session, user2)
        await session.commit()

        # Second workspace should have a different slug with random suffix
        assert workspace2.slug != workspace1.slug
        assert workspace2.slug.startswith("testuser-")
        assert len(workspace2.slug) > len("testuser-")  # Has suffix
        assert workspace2.is_personal is True


@pytest.mark.asyncio
async def test_create_personal_workspace_allows_duplicate_names(async_engine):
    """Test that personal workspaces can have duplicate names (only slug must be unique)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from stardag_api.auth.dependencies import create_personal_workspace_for_user

    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    async with async_session_maker() as session:
        # Create two users with the same display name
        user1 = User(
            external_id="user-dup-name-1",
            email="alice@company1.com",
            display_name="Anders Huss",
        )
        session.add(user1)
        await session.commit()
        await session.refresh(user1)

        workspace1 = await create_personal_workspace_for_user(session, user1)
        await session.commit()

        user2 = User(
            external_id="user-dup-name-2",
            email="bob@company2.com",
            display_name="Anders Huss",
        )
        session.add(user2)
        await session.commit()
        await session.refresh(user2)

        workspace2 = await create_personal_workspace_for_user(session, user2)
        await session.commit()

        # Both workspaces should have the same name (duplicate allowed)
        assert workspace1.name == "Anders Huss's Workspace"
        assert workspace2.name == "Anders Huss's Workspace"

        # But slugs must be unique
        assert workspace1.slug != workspace2.slug


@pytest.mark.asyncio
async def test_create_personal_workspace_uses_email_prefix_for_name_fallback(
    async_engine,
):
    """Test that workspace name falls back to email prefix when display_name is None."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from stardag_api.auth.dependencies import create_personal_workspace_for_user

    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    async with async_session_maker() as session:
        user = User(
            external_id="user-no-display-name",
            email="johnny@example.com",
            display_name=None,  # No display name
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        workspace = await create_personal_workspace_for_user(session, user)
        await session.commit()

        assert workspace.name == "johnny's Workspace"
        assert workspace.slug == "johnny"


@pytest.mark.asyncio
async def test_me_invites_endpoint(async_engine):
    """Test that /api/v1/ui/me/invites returns pending invites.

    Note: /me/invites is a bootstrap endpoint that accepts Keycloak tokens
    (not internal tokens).
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from stardag_api.models import Invite
    from stardag_api.models.enums import InviteStatus

    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    # Create a user and a pending invite
    async with async_session_maker() as session:
        inviter = User(
            external_id="inviter-user",
            email="inviter@example.com",
            display_name="Inviter",
        )
        session.add(inviter)

        invitee = User(
            external_id="invitee-user",
            email="invitee@example.com",
            display_name="Invitee",
        )
        session.add(invitee)
        await session.commit()
        await session.refresh(inviter)
        await session.refresh(invitee)
        mock_invitee = invitee

        workspace = Workspace(
            name="Inviting Workspace",
            slug="inviting-workspace",
        )
        session.add(workspace)
        await session.commit()
        await session.refresh(workspace)

        invite = Invite(
            email="invitee@example.com",
            workspace_id=workspace.id,
            invited_by_id=inviter.id,
            role=WorkspaceRole.MEMBER,
            status=InviteStatus.PENDING,
        )
        session.add(invite)
        await session.commit()

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_current_user_flexible():
        return mock_invitee

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
        assert len(data) == 1
        assert data[0]["workspace_name"] == "Inviting Workspace"
        assert data[0]["role"] == "member"
        assert data[0]["invited_by_email"] == "inviter@example.com"
    finally:
        app.dependency_overrides.clear()
