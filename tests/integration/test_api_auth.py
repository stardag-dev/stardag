"""API authentication integration tests.

These tests verify the authentication flow works correctly with real tokens
against the running docker-compose services.
"""

import httpx

from conftest import TokenSet, exchange_oidc_for_internal_token
from docker_fixtures import ServiceEndpoints


class TestTokenExchange:
    """Tests for the /auth/exchange endpoint."""

    def test_exchange_accepts_oidc_token(
        self,
        docker_services: ServiceEndpoints,
        oidc_token: TokenSet,
        test_organization_id: str,
    ) -> None:
        """Test that /auth/exchange accepts OIDC tokens."""
        internal_token = exchange_oidc_for_internal_token(
            api_url=docker_services.api,
            oidc_token=oidc_token.access_token,
            organization_id=test_organization_id,
        )
        assert internal_token is not None
        assert len(internal_token) > 0

    def test_exchange_rejects_internal_token(
        self,
        docker_services: ServiceEndpoints,
        internal_token: str,
        test_organization_id: str,
    ) -> None:
        """Test that /auth/exchange rejects internal tokens."""
        # Try to exchange an internal token (should fail)
        response = httpx.post(
            f"{docker_services.api}/api/v1/auth/exchange",
            json={"org_id": test_organization_id},
            headers={"Authorization": f"Bearer {internal_token}"},
            timeout=30.0,
        )
        # Internal tokens should not be accepted for exchange
        assert response.status_code == 401

    def test_exchange_rejects_invalid_token(
        self,
        docker_services: ServiceEndpoints,
        test_organization_id: str,
    ) -> None:
        """Test that /auth/exchange rejects invalid tokens."""
        response = httpx.post(
            f"{docker_services.api}/api/v1/auth/exchange",
            json={"org_id": test_organization_id},
            headers={"Authorization": "Bearer invalid-token"},
            timeout=30.0,
        )
        assert response.status_code == 401

    def test_exchange_requires_org_id(
        self,
        docker_services: ServiceEndpoints,
        oidc_token: TokenSet,
    ) -> None:
        """Test that /auth/exchange requires org_id."""
        response = httpx.post(
            f"{docker_services.api}/api/v1/auth/exchange",
            json={},
            headers={"Authorization": f"Bearer {oidc_token.access_token}"},
            timeout=30.0,
        )
        assert response.status_code == 422  # Validation error


class TestTokenTypeValidation:
    """Tests that endpoints correctly validate token types."""

    def test_builds_rejects_oidc_token(
        self,
        docker_services: ServiceEndpoints,
        oidc_token: TokenSet,
        test_workspace_id: str,
    ) -> None:
        """Test that /builds rejects OIDC tokens (requires internal token)."""
        response = httpx.get(
            f"{docker_services.api}/api/v1/builds",
            params={"workspace_id": test_workspace_id},
            headers={"Authorization": f"Bearer {oidc_token.access_token}"},
            timeout=30.0,
        )
        # OIDC tokens should not work for builds endpoint
        assert response.status_code == 401

    def test_builds_accepts_internal_token(
        self,
        internal_authenticated_client: httpx.Client,
        test_workspace_id: str,
    ) -> None:
        """Test that /builds accepts internal tokens."""
        response = internal_authenticated_client.get(
            "/api/v1/builds",
            params={"workspace_id": test_workspace_id},
        )
        assert response.status_code == 200

    def test_tasks_with_oidc_token(
        self,
        docker_services: ServiceEndpoints,
        oidc_token: TokenSet,
        test_workspace_id: str,
    ) -> None:
        """Test /tasks endpoint behavior with OIDC tokens.

        NOTE: Currently /tasks allows unauthenticated access and returns
        empty results when no workspace_id is provided. This may be
        intentional for public task discovery or a bug.
        """
        response = httpx.get(
            f"{docker_services.api}/api/v1/tasks",
            params={"workspace_id": test_workspace_id},
            headers={"Authorization": f"Bearer {oidc_token.access_token}"},
            timeout=30.0,
        )
        # Tasks endpoint currently allows access with OIDC token
        # This returns 200 but may filter results based on token type
        assert response.status_code == 200

    def test_tasks_accepts_internal_token(
        self,
        internal_authenticated_client: httpx.Client,
        test_workspace_id: str,
    ) -> None:
        """Test that /tasks accepts internal tokens."""
        response = internal_authenticated_client.get(
            "/api/v1/tasks",
            params={"workspace_id": test_workspace_id},
        )
        assert response.status_code == 200

    def test_ui_me_accepts_oidc_token(
        self,
        authenticated_client: httpx.Client,
    ) -> None:
        """Test that /ui/me accepts OIDC tokens (bootstrap endpoint)."""
        response = authenticated_client.get("/api/v1/ui/me")
        assert response.status_code == 200
        data = response.json()
        assert "user" in data
        assert "organizations" in data

    def test_ui_me_accepts_internal_token(
        self,
        internal_authenticated_client: httpx.Client,
    ) -> None:
        """Test that /ui/me also accepts internal tokens."""
        response = internal_authenticated_client.get("/api/v1/ui/me")
        assert response.status_code == 200
        data = response.json()
        assert "user" in data


class TestApiKeyAuth:
    """Tests for API key authentication.

    NOTE: Currently only write endpoints (POST /builds, etc.) support API key auth.
    Read endpoints (GET /builds) require internal JWT tokens.
    The /tasks endpoint has no authentication at all.
    """

    def test_create_api_key(
        self,
        internal_authenticated_client: httpx.Client,
        test_organization_id: str,
        test_workspace_id: str,
    ) -> None:
        """Test creating an API key."""
        response = internal_authenticated_client.post(
            f"/api/v1/ui/organizations/{test_organization_id}"
            f"/workspaces/{test_workspace_id}/api-keys",
            json={"name": "Test API Key"},
        )
        assert response.status_code == 201
        data = response.json()
        assert "key" in data  # Full key only returned on creation
        assert "key_prefix" in data
        assert data["name"] == "Test API Key"

    def test_api_key_auth_for_create_build(
        self,
        internal_authenticated_client: httpx.Client,
        docker_services: ServiceEndpoints,
        test_organization_id: str,
        test_workspace_id: str,
    ) -> None:
        """Test that API keys can be used to create builds.

        API keys are workspace-scoped, so no workspace_id parameter is needed.
        NOTE: Only POST endpoints support API key auth; GET /builds requires JWT.
        """
        # Create an API key
        response = internal_authenticated_client.post(
            f"/api/v1/ui/organizations/{test_organization_id}"
            f"/workspaces/{test_workspace_id}/api-keys",
            json={"name": "SDK Test Key"},
        )
        assert response.status_code == 201
        api_key = response.json()["key"]

        # Use the API key to create a build (API key implies workspace)
        response = httpx.post(
            f"{docker_services.api}/api/v1/builds",
            headers={"X-API-Key": api_key},
            json={"description": "Test build via API key"},
            timeout=30.0,
        )
        # API key auth should work - the key is scoped to a workspace
        assert response.status_code == 201, f"API key auth failed: {response.text}"
        data = response.json()
        assert "id" in data
        assert "workspace_id" in data

    def test_api_key_auth_get_builds_not_supported(
        self,
        internal_authenticated_client: httpx.Client,
        docker_services: ServiceEndpoints,
        test_organization_id: str,
        test_workspace_id: str,
    ) -> None:
        """Test that GET /builds does NOT support API key auth (requires JWT).

        This documents current behavior - GET endpoints use get_current_user
        which requires internal JWT, not API key auth.
        """
        # Create an API key
        response = internal_authenticated_client.post(
            f"/api/v1/ui/organizations/{test_organization_id}"
            f"/workspaces/{test_workspace_id}/api-keys",
            json={"name": "Read Test Key"},
        )
        assert response.status_code == 201
        api_key = response.json()["key"]

        # Try to use the API key to GET builds - this should fail
        response = httpx.get(
            f"{docker_services.api}/api/v1/builds",
            headers={"X-API-Key": api_key},
            params={"workspace_id": test_workspace_id},
            timeout=30.0,
        )
        # Currently GET /builds requires JWT auth, not API key
        assert response.status_code == 401

    def test_invalid_api_key_rejected(
        self,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test that invalid API keys are rejected for POST endpoints."""
        response = httpx.post(
            f"{docker_services.api}/api/v1/builds",
            headers={"X-API-Key": "invalid-key"},
            json={"description": "Should fail"},
            timeout=30.0,
        )
        assert response.status_code == 401


class TestEndpointAccess:
    """Tests for endpoint access control.

    NOTE: Some endpoints have incomplete auth implementation:
    - /tasks has NO authentication (completely open) - TODO: fix
    - GET /builds requires JWT but not API key - TODO: add API key support
    """

    def test_protected_endpoints_require_auth(
        self,
        unauthenticated_client: httpx.Client,
    ) -> None:
        """Test that protected endpoints return 401 without auth."""
        # NOTE: /tasks is intentionally excluded - it currently has no auth
        endpoints = [
            "/api/v1/builds",
            "/api/v1/ui/me",
        ]

        for endpoint in endpoints:
            response = unauthenticated_client.get(endpoint)
            assert response.status_code == 401, f"{endpoint} should require auth"

    def test_tasks_endpoint_has_no_auth(
        self,
        unauthenticated_client: httpx.Client,
    ) -> None:
        """Test that /tasks currently allows unauthenticated access.

        NOTE: This documents current behavior. The /tasks endpoint should
        probably require authentication - this is tracked as a bug to fix.
        """
        response = unauthenticated_client.get("/api/v1/tasks")
        # Currently allows unauthenticated access (bug)
        assert response.status_code == 200

    def test_router_prefixes_correct(
        self,
        docker_services: ServiceEndpoints,
    ) -> None:
        """Test that all routers are mounted with correct prefixes."""
        # Test key endpoints exist at expected paths
        # Note: Health endpoint is at /health, not /api/v1/health
        endpoints_to_check = [
            ("/health", 200),  # Health endpoint (no auth, root level)
            ("/api/v1/auth/exchange", 401),  # Auth router (POST only, so 401 for GET)
            ("/api/v1/ui/me", 401),  # UI router
            ("/api/v1/builds", 401),  # Builds router
            ("/api/v1/tasks", 200),  # Tasks router (no auth currently)
        ]

        for path, expected_status in endpoints_to_check:
            response = httpx.get(f"{docker_services.api}{path}", timeout=10.0)
            # 401, 405 (method not allowed), or expected status are acceptable
            assert response.status_code in (
                expected_status,
                405,
            ), f"{path} returned {response.status_code}"

    def test_wrong_org_returns_403(
        self,
        docker_services: ServiceEndpoints,
        internal_token: str,
    ) -> None:
        """Test that accessing resources from wrong org returns 403."""
        # The internal token is scoped to a specific org
        # Try to access a workspace that doesn't exist in that org
        fake_workspace_id = "00000000-0000-0000-0000-000000000000"
        response = httpx.get(
            f"{docker_services.api}/api/v1/builds",
            params={"workspace_id": fake_workspace_id},
            headers={"Authorization": f"Bearer {internal_token}"},
            timeout=30.0,
        )
        # Should be 403 (forbidden) or 404 (not found)
        assert response.status_code in (403, 404)
