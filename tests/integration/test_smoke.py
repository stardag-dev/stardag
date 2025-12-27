"""Smoke tests to verify the integration test infrastructure works.

These tests verify basic connectivity to docker-compose services.
"""

import httpx

from docker_fixtures import ServiceEndpoints


class TestDockerServices:
    """Tests for docker service availability."""

    def test_api_health(self, docker_services: ServiceEndpoints) -> None:
        """Test that the API health endpoint responds."""
        response = httpx.get(f"{docker_services.api}/health", timeout=10.0)
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "healthy"

    def test_keycloak_health(self, docker_services: ServiceEndpoints) -> None:
        """Test that Keycloak realm is accessible.

        Note: The health endpoint is on management port 9000 which isn't exposed,
        so we check the OIDC config endpoint instead.
        """
        response = httpx.get(
            f"{docker_services.oidc_issuer}/.well-known/openid-configuration",
            timeout=10.0,
        )
        assert response.status_code == 200
        data = response.json()
        assert "token_endpoint" in data

    def test_keycloak_realm_exists(self, docker_services: ServiceEndpoints) -> None:
        """Test that the stardag realm is configured in Keycloak."""
        response = httpx.get(
            f"{docker_services.oidc_issuer}/.well-known/openid-configuration",
            timeout=10.0,
        )
        assert response.status_code == 200
        data = response.json()
        assert "authorization_endpoint" in data
        assert "token_endpoint" in data

    def test_ui_accessible(self, docker_services: ServiceEndpoints) -> None:
        """Test that the UI serves content."""
        response = httpx.get(docker_services.ui, timeout=10.0)
        assert response.status_code == 200
        assert '<div id="root"' in response.text


class TestAuthFixtures:
    """Tests for authentication fixtures."""

    def test_oidc_token_fixture(
        self,
        docker_services: ServiceEndpoints,
        oidc_token,
    ) -> None:
        """Test that we can get an OIDC token for the test user."""
        assert oidc_token.access_token is not None
        assert len(oidc_token.access_token) > 0

    def test_authenticated_client_can_access_me(
        self,
        authenticated_client: httpx.Client,
    ) -> None:
        """Test that authenticated client can access /ui/me endpoint."""
        response = authenticated_client.get("/api/v1/ui/me")
        assert response.status_code == 200
        data = response.json()
        assert "user" in data
        assert "organizations" in data

    def test_unauthenticated_client_rejected(
        self,
        unauthenticated_client: httpx.Client,
    ) -> None:
        """Test that unauthenticated requests are rejected."""
        response = unauthenticated_client.get("/api/v1/builds")
        assert response.status_code == 401


class TestOrgAndWorkspaceFixtures:
    """Tests for organization and workspace fixtures."""

    def test_organization_fixture(self, test_organization_id: str) -> None:
        """Test that organization fixture returns a valid ID."""
        assert test_organization_id is not None
        assert len(test_organization_id) > 0

    def test_internal_token_fixture(self, internal_token: str) -> None:
        """Test that internal token fixture returns a valid token."""
        assert internal_token is not None
        assert len(internal_token) > 0

    def test_internal_client_can_access_builds(
        self,
        internal_authenticated_client: httpx.Client,
        test_workspace_id: str,
    ) -> None:
        """Test that internal auth client can access builds endpoint."""
        response = internal_authenticated_client.get(
            "/api/v1/builds",
            params={"workspace_id": test_workspace_id},
        )
        assert response.status_code == 200
        data = response.json()
        assert "items" in data or "total" in data

    def test_workspace_fixture(self, test_workspace_id: str) -> None:
        """Test that workspace fixture returns a valid ID."""
        assert test_workspace_id is not None
        assert len(test_workspace_id) > 0
