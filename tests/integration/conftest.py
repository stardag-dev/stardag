"""Shared fixtures for integration tests.

This module provides common fixtures for testing against the running
docker-compose services.
"""

import logging
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass

import httpx
import pytest

from docker_fixtures import (
    TEST_USER_EMAIL,
    TEST_USER_PASSWORD,
    TEST_USER_USERNAME,
    ServiceEndpoints,
    docker_logs_on_failure,  # noqa: F401 - imported for pytest
    docker_services,  # noqa: F401 - imported for pytest
    pytest_runtest_makereport,  # noqa: F401 - imported for pytest
)

logger = logging.getLogger(__name__)

# Configure logging for tests
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


@dataclass
class TestUser:
    """Test user credentials and info."""

    username: str = TEST_USER_USERNAME
    email: str = TEST_USER_EMAIL
    password: str = TEST_USER_PASSWORD


@dataclass
class TokenSet:
    """OAuth token set."""

    access_token: str
    refresh_token: str | None = None
    id_token: str | None = None
    token_type: str = "Bearer"
    expires_in: int | None = None


# --- HTTP Client Fixtures ---


@pytest.fixture
async def http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async HTTP client for API requests."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        yield client


@pytest.fixture
def sync_http_client() -> Generator[httpx.Client, None, None]:
    """Sync HTTP client for API requests."""
    with httpx.Client(timeout=30.0) as client:
        yield client


# --- Test User Fixtures ---


@pytest.fixture(scope="session")
def test_user() -> TestUser:
    """Get test user credentials."""
    return TestUser()


# --- Keycloak/OIDC Token Fixtures ---


def get_oidc_token_via_password_grant(
    keycloak_url: str,
    username: str,
    password: str,
    client_id: str = "stardag-test",
    realm: str = "stardag",
) -> TokenSet:
    """Get an OIDC token using the password grant (for testing only).

    Note: This uses the direct access grant which must be enabled on the client.
    For production, use authorization code flow with PKCE.
    """
    token_url = f"{keycloak_url}/realms/{realm}/protocol/openid-connect/token"

    response = httpx.post(
        token_url,
        data={
            "grant_type": "password",
            "client_id": client_id,
            "username": username,
            "password": password,
            "scope": "openid profile email",
        },
        timeout=30.0,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to get OIDC token: {response.status_code} - {response.text}"
        )

    data = response.json()
    return TokenSet(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        id_token=data.get("id_token"),
        token_type=data.get("token_type", "Bearer"),
        expires_in=data.get("expires_in"),
    )


@pytest.fixture
def oidc_token(
    docker_services: ServiceEndpoints,  # noqa: F811
    test_user: TestUser,
) -> TokenSet:
    """Get an OIDC token for the test user.

    Note: This requires direct access grants to be enabled on the Keycloak client.
    """
    return get_oidc_token_via_password_grant(
        keycloak_url=docker_services.keycloak,
        username=test_user.username,
        password=test_user.password,
    )


# --- API Auth Fixtures ---


def exchange_oidc_for_internal_token(
    api_url: str,
    oidc_token: str,
    organization_id: str,
) -> str:
    """Exchange an OIDC token for an internal (org-scoped) token.

    Args:
        api_url: Base API URL
        oidc_token: OIDC access token
        organization_id: Organization ID to scope the token to

    Returns:
        Internal access token
    """
    exchange_url = f"{api_url}/api/v1/auth/exchange"

    response = httpx.post(
        exchange_url,
        json={"org_id": organization_id},
        headers={"Authorization": f"Bearer {oidc_token}"},
        timeout=30.0,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to exchange token: {response.status_code} - {response.text}"
        )

    data = response.json()
    return data["access_token"]


@pytest.fixture
def authenticated_client(
    docker_services: ServiceEndpoints,  # noqa: F811
    oidc_token: TokenSet,
) -> Generator[httpx.Client, None, None]:
    """HTTP client authenticated with OIDC token.

    This client can access bootstrap endpoints like /ui/me.
    For org-scoped endpoints, use internal_authenticated_client instead.
    """
    with httpx.Client(
        base_url=docker_services.api,
        headers={"Authorization": f"Bearer {oidc_token.access_token}"},
        timeout=30.0,
    ) as client:
        yield client


# --- Organization/Workspace Fixtures ---


@pytest.fixture
def test_organization_id(
    authenticated_client: httpx.Client,
) -> str:
    """Get or create a test organization and return its ID.

    Uses the /ui/me endpoint to get the user's organizations,
    or creates one if none exist.
    """
    # Get user profile with organizations
    response = authenticated_client.get("/api/v1/ui/me")
    if response.status_code != 200:
        raise RuntimeError(f"Failed to get user profile: {response.text}")

    data = response.json()
    organizations = data.get("organizations", [])

    if organizations:
        # Use the first organization
        org_id = organizations[0]["id"]
        logger.info("Using existing organization: %s", org_id)
        return org_id

    # Create a new organization
    response = authenticated_client.post(
        "/api/v1/ui/organizations",
        json={
            "name": "Test Organization",
            "slug": "test-org",
        },
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Failed to create organization: {response.text}")

    org_id = response.json()["id"]
    logger.info("Created test organization: %s", org_id)
    return org_id


@pytest.fixture
def internal_token(
    docker_services: ServiceEndpoints,  # noqa: F811
    oidc_token: TokenSet,
    test_organization_id: str,
) -> str:
    """Get an internal (org-scoped) token for the test user."""
    return exchange_oidc_for_internal_token(
        api_url=docker_services.api,
        oidc_token=oidc_token.access_token,
        organization_id=test_organization_id,
    )


@pytest.fixture
def internal_authenticated_client(
    docker_services: ServiceEndpoints,  # noqa: F811
    internal_token: str,
) -> Generator[httpx.Client, None, None]:
    """HTTP client authenticated with an internal (org-scoped) token.

    This client can access all protected endpoints.
    """
    with httpx.Client(
        base_url=docker_services.api,
        headers={"Authorization": f"Bearer {internal_token}"},
        timeout=30.0,
    ) as client:
        yield client


@pytest.fixture
def test_workspace_id(
    internal_authenticated_client: httpx.Client,
    test_organization_id: str,
) -> str:
    """Get or create a test workspace and return its ID."""
    # List workspaces in the organization
    response = internal_authenticated_client.get(
        f"/api/v1/ui/organizations/{test_organization_id}/workspaces"
    )
    if response.status_code != 200:
        raise RuntimeError(f"Failed to list workspaces: {response.text}")

    workspaces = response.json()
    if workspaces:
        workspace_id = workspaces[0]["id"]
        logger.info("Using existing workspace: %s", workspace_id)
        return workspace_id

    # Create a new workspace
    response = internal_authenticated_client.post(
        f"/api/v1/ui/organizations/{test_organization_id}/workspaces",
        json={
            "name": "Test Workspace",
            "slug": "test-workspace",
        },
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Failed to create workspace: {response.text}")

    workspace_id = response.json()["id"]
    logger.info("Created test workspace: %s", workspace_id)
    return workspace_id


# --- Unauthenticated Client ---


@pytest.fixture
def unauthenticated_client(
    docker_services: ServiceEndpoints,  # noqa: F811
) -> Generator[httpx.Client, None, None]:
    """HTTP client without authentication."""
    with httpx.Client(
        base_url=docker_services.api,
        timeout=30.0,
    ) as client:
        yield client
