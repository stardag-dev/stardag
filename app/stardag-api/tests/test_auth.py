"""Tests for authentication flow."""

import time
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth.dependencies import get_or_create_user, get_token
from stardag_api.auth.jwt import AuthenticationError, JWTValidator, TokenPayload
from stardag_api.db import get_db
from stardag_api.main import app
from stardag_api.models import Organization, OrganizationMember, User
from stardag_api.models.enums import OrganizationRole


# --- TokenPayload tests ---


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


# --- JWTValidator tests ---


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


# --- get_or_create_user tests ---


@pytest.mark.asyncio
async def test_get_or_create_user_creates_new_user(async_session: AsyncSession):
    """Test that get_or_create_user creates a new user."""
    token = TokenPayload(
        sub="new-user-external-id",
        email="newuser@example.com",
        email_verified=True,
        name="New User",
        given_name="New",
        family_name="User",
        preferred_username="newuser",
        iss="https://example.com",
        aud="stardag-ui",
        exp=1234567890,
        iat=1234567800,
        raw={},
    )

    user = await get_or_create_user(async_session, token)

    assert user.external_id == "new-user-external-id"
    assert user.email == "newuser@example.com"
    assert user.display_name == "New User"

    # Verify user was persisted
    result = await async_session.execute(
        select(User).where(User.external_id == "new-user-external-id")
    )
    persisted_user = result.scalar_one()
    assert persisted_user.id == user.id


@pytest.mark.asyncio
async def test_get_or_create_user_returns_existing(async_session: AsyncSession):
    """Test that get_or_create_user returns existing user."""
    # Create a user first
    existing_user = User(
        external_id="existing-external-id",
        email="existing@example.com",
        display_name="Existing User",
    )
    async_session.add(existing_user)
    await async_session.commit()
    await async_session.refresh(existing_user)

    token = TokenPayload(
        sub="existing-external-id",
        email="existing@example.com",
        email_verified=True,
        name="Existing User",
        given_name=None,
        family_name=None,
        preferred_username=None,
        iss="https://example.com",
        aud="stardag-ui",
        exp=1234567890,
        iat=1234567800,
        raw={},
    )

    user = await get_or_create_user(async_session, token)

    assert user.id == existing_user.id
    assert user.external_id == "existing-external-id"


@pytest.mark.asyncio
async def test_get_or_create_user_updates_email(async_session: AsyncSession):
    """Test that get_or_create_user updates email if changed."""
    # Create a user with old email
    existing_user = User(
        external_id="user-with-changed-email",
        email="old@example.com",
        display_name="Test User",
    )
    async_session.add(existing_user)
    await async_session.commit()

    token = TokenPayload(
        sub="user-with-changed-email",
        email="new@example.com",  # Changed email
        email_verified=True,
        name="Test User",
        given_name=None,
        family_name=None,
        preferred_username=None,
        iss="https://example.com",
        aud="stardag-ui",
        exp=1234567890,
        iat=1234567800,
        raw={},
    )

    user = await get_or_create_user(async_session, token)

    assert user.email == "new@example.com"


@pytest.mark.asyncio
async def test_get_or_create_user_requires_email_for_new(async_session: AsyncSession):
    """Test that get_or_create_user requires email for new users."""
    token = TokenPayload(
        sub="user-without-email",
        email=None,  # No email
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

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await get_or_create_user(async_session, token)

    assert exc_info.value.status_code == 400
    assert "email claim" in str(exc_info.value.detail)


# --- /api/v1/ui/me endpoint tests ---


def _create_mock_token_payload(
    sub: str = "test-user-sub",
    email: str = "test@example.com",
    name: str = "Test User",
) -> TokenPayload:
    """Create a mock token payload for testing."""
    return TokenPayload(
        sub=sub,
        email=email,
        email_verified=True,
        name=name,
        given_name=None,
        family_name=None,
        preferred_username=None,
        iss="https://example.com",
        aud="stardag-ui",
        exp=9999999999,
        iat=1234567800,
        raw={},
    )


@pytest.mark.asyncio
async def test_me_endpoint_requires_auth(unauthenticated_client: AsyncClient):
    """Test that /api/v1/ui/me requires authentication."""
    response = await unauthenticated_client.get("/api/v1/ui/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_me_endpoint_returns_user_profile(async_engine):
    """Test that /api/v1/ui/me returns user profile."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    mock_token = _create_mock_token_payload()

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        from httpx import ASGITransport, AsyncClient as HttpxAsyncClient

        async with HttpxAsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/ui/me")

        assert response.status_code == 200
        data = response.json()
        assert "user" in data
        assert "organizations" in data
        assert data["user"]["external_id"] == "test-user-sub"
        assert data["user"]["email"] == "test@example.com"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_me_endpoint_returns_organizations(async_engine):
    """Test that /api/v1/ui/me returns user's organizations."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    # Create a user with organization membership
    async with async_session_maker() as session:
        user = User(
            external_id="user-with-org",
            email="orguser@example.com",
            display_name="Org User",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        user_id = user.id

        org = Organization(
            name="Test Org",
            slug="test-org",
        )
        session.add(org)
        await session.commit()
        await session.refresh(org)
        org_id = org.id

        membership = OrganizationMember(
            organization_id=org_id,
            user_id=user_id,
            role=OrganizationRole.ADMIN,
        )
        session.add(membership)
        await session.commit()

    mock_token = _create_mock_token_payload(
        sub="user-with-org",
        email="orguser@example.com",
        name="Org User",
    )

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        from httpx import ASGITransport, AsyncClient as HttpxAsyncClient

        async with HttpxAsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/ui/me")

        assert response.status_code == 200
        data = response.json()
        assert len(data["organizations"]) == 1
        assert data["organizations"][0]["name"] == "Test Org"
        assert data["organizations"][0]["slug"] == "test-org"
        assert data["organizations"][0]["role"] == "admin"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_me_invites_endpoint(async_engine):
    """Test that /api/v1/ui/me/invites returns pending invites."""
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
        await session.commit()
        await session.refresh(inviter)

        org = Organization(
            name="Inviting Org",
            slug="inviting-org",
        )
        session.add(org)
        await session.commit()
        await session.refresh(org)

        invite = Invite(
            email="invitee@example.com",
            organization_id=org.id,
            invited_by_id=inviter.id,
            role=OrganizationRole.MEMBER,
            status=InviteStatus.PENDING,
        )
        session.add(invite)
        await session.commit()

    mock_token = _create_mock_token_payload(
        sub="invitee-user",
        email="invitee@example.com",
        name="Invitee",
    )

    async def override_get_db():
        async with async_session_maker() as session:
            yield session

    async def override_get_token():
        return mock_token

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token] = override_get_token

    try:
        from httpx import ASGITransport, AsyncClient as HttpxAsyncClient

        async with HttpxAsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/ui/me/invites")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["organization_name"] == "Inviting Org"
        assert data[0]["role"] == "member"
        assert data[0]["invited_by_email"] == "inviter@example.com"
    finally:
        app.dependency_overrides.clear()
