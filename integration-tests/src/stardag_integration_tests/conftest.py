"""Shared fixtures for integration tests.

This module provides common fixtures for testing against the running
docker-compose services.
"""

import logging
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass

import httpx
import pytest

from .docker_fixtures import (
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
    workspace_id: str,
) -> str:
    """Exchange an OIDC token for an internal (workspace-scoped) token.

    Args:
        api_url: Base API URL
        oidc_token: OIDC access token
        workspace_id: Workspace ID to scope the token to

    Returns:
        Internal access token
    """
    exchange_url = f"{api_url}/api/v1/auth/exchange"

    response = httpx.post(
        exchange_url,
        json={"workspace_id": workspace_id},
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
    For workspace-scoped endpoints, use internal_authenticated_client instead.
    """
    with httpx.Client(
        base_url=docker_services.api,
        headers={"Authorization": f"Bearer {oidc_token.access_token}"},
        timeout=30.0,
    ) as client:
        yield client


# --- Workspace/Environment Fixtures ---


@pytest.fixture
def test_workspace_id(
    authenticated_client: httpx.Client,
) -> str:
    """Get or create a test workspace and return its ID.

    Uses the /ui/me endpoint to get the user's workspaces,
    or creates one if none exist.
    """
    # Get user profile with workspaces
    response = authenticated_client.get("/api/v1/ui/me")
    if response.status_code != 200:
        raise RuntimeError(f"Failed to get user profile: {response.text}")

    data = response.json()
    workspaces = data.get("workspaces", [])

    if workspaces:
        # Use the first workspace
        workspace_id = workspaces[0]["id"]
        logger.info("Using existing workspace: %s", workspace_id)
        return workspace_id

    # Create a new workspace
    response = authenticated_client.post(
        "/api/v1/ui/workspaces",
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


@pytest.fixture
def internal_token(
    docker_services: ServiceEndpoints,  # noqa: F811
    oidc_token: TokenSet,
    test_workspace_id: str,
) -> str:
    """Get an internal (workspace-scoped) token for the test user."""
    return exchange_oidc_for_internal_token(
        api_url=docker_services.api,
        oidc_token=oidc_token.access_token,
        workspace_id=test_workspace_id,
    )


@pytest.fixture
def internal_authenticated_client(
    docker_services: ServiceEndpoints,  # noqa: F811
    internal_token: str,
) -> Generator[httpx.Client, None, None]:
    """HTTP client authenticated with an internal (workspace-scoped) token.

    This client can access all protected endpoints.
    """
    with httpx.Client(
        base_url=docker_services.api,
        headers={"Authorization": f"Bearer {internal_token}"},
        timeout=30.0,
    ) as client:
        yield client


@pytest.fixture
def test_environment_id(
    internal_authenticated_client: httpx.Client,
    test_workspace_id: str,
) -> str:
    """Get or create a test environment and return its ID."""
    # List environments in the workspace
    response = internal_authenticated_client.get(
        f"/api/v1/ui/workspaces/{test_workspace_id}/environments"
    )
    if response.status_code != 200:
        raise RuntimeError(f"Failed to list environments: {response.text}")

    environments = response.json()
    if environments:
        environment_id = environments[0]["id"]
        logger.info("Using existing environment: %s", environment_id)
        return environment_id

    # Create a new environment
    response = internal_authenticated_client.post(
        f"/api/v1/ui/workspaces/{test_workspace_id}/environments",
        json={
            "name": "Test Environment",
            "slug": "test-environment",
        },
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Failed to create environment: {response.text}")

    environment_id = response.json()["id"]
    logger.info("Created test environment: %s", environment_id)
    return environment_id


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
