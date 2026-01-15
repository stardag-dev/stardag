"""API authentication integration tests.

These tests verify the authentication flow works correctly with real tokens
against the running docker-compose services.
"""

import httpx

from stardag_integration_tests.conftest import (
    TokenSet,
    exchange_oidc_for_internal_token,
)
from stardag_integration_tests.docker_fixtures import ServiceEndpoints


class TestTokenExchange:
    """Tests for the /auth/exchange endpoint."""

    def test_exchange_accepts_oidc_token(
        self,
        docker_services: ServiceEndpoints,
        oidc_token: TokenSet,
        test_workspace_id: str,
    ) -> None:
        """Test that /auth/exchange accepts OIDC tokens."""
        internal_token = exchange_oidc_for_internal_token(
            api_url=docker_services.api,
            oidc_token=oidc_token.access_token,
            workspace_id=test_workspace_id,
        )
        assert internal_token is not None
        assert len(internal_token) > 0

    def test_exchange_rejects_internal_token(
        self,
        docker_services: ServiceEndpoints,
        internal_token: str,
        test_workspace_id: str,
    ) -> None:
        """Test that /auth/exchange rejects internal tokens."""
        # Try to exchange an internal token (should fail)
        response = httpx.post(
            f"{docker_services.api}/api/v1/auth/exchange",
            json={"workspace_id": test_workspace_id},
            headers={"Authorization": f"Bearer {internal_token}"},
            timeout=30.0,
        )
        # Internal tokens should not be accepted for exchange
        assert response.status_code == 401

    def test_exchange_rejects_invalid_token(
        self,
        docker_services: ServiceEndpoints,
        test_workspace_id: str,
    ) -> None:
        """Test that /auth/exchange rejects invalid tokens."""
        response = httpx.post(
            f"{docker_services.api}/api/v1/auth/exchange",
            json={"workspace_id": test_workspace_id},
            headers={"Authorization": "Bearer invalid-token"},
            timeout=30.0,
        )
        assert response.status_code == 401

    def test_exchange_requires_workspace_id(
        self,
        docker_services: ServiceEndpoints,
        oidc_token: TokenSet,
    ) -> None:
        """Test that /auth/exchange requires workspace_id."""
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
        test_environment_id: str,
    ) -> None:
        """Test that /builds rejects OIDC tokens (requires internal token)."""
        response = httpx.get(
            f"{docker_services.api}/api/v1/builds",
            params={"environment_id": test_environment_id},
            headers={"Authorization": f"Bearer {oidc_token.access_token}"},
            timeout=30.0,
        )
        # OIDC tokens should not work for builds endpoint
        assert response.status_code == 401

    def test_builds_accepts_internal_token(
        self,
        internal_authenticated_client: httpx.Client,
        test_environment_id: str,
    ) -> None:
        """Test that /builds accepts internal tokens."""
        response = internal_authenticated_client.get(
            "/api/v1/builds",
            params={"environment_id": test_environment_id},
        )
        assert response.status_code == 200

    def test_tasks_rejects_oidc_token(
        self,
        docker_services: ServiceEndpoints,
        oidc_token: TokenSet,
        test_environment_id: str,
    ) -> None:
        """Test /tasks endpoint rejects OIDC tokens (requires internal token or API key)."""
        response = httpx.get(
            f"{docker_services.api}/api/v1/tasks",
            params={"environment_id": test_environment_id},
            headers={"Authorization": f"Bearer {oidc_token.access_token}"},
            timeout=30.0,
        )
        # Tasks endpoint requires internal token or API key
        assert response.status_code == 401

    def test_tasks_accepts_internal_token(
        self,
        internal_authenticated_client: httpx.Client,
        test_environment_id: str,
    ) -> None:
        """Test that /tasks accepts internal tokens with environment_id."""
        response = internal_authenticated_client.get(
            "/api/v1/tasks",
            params={"environment_id": test_environment_id},
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
        assert "workspaces" in data

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

    API keys provide environment-scoped authentication for SDK operations.
    Both read and write endpoints support API key auth.
    """

    def test_create_api_key(
        self,
        internal_authenticated_client: httpx.Client,
        test_workspace_id: str,
        test_environment_id: str,
    ) -> None:
        """Test creating an API key."""
        response = internal_authenticated_client.post(
            f"/api/v1/ui/workspaces/{test_workspace_id}"
            f"/environments/{test_environment_id}/api-keys",
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
        test_workspace_id: str,
        test_environment_id: str,
    ) -> None:
        """Test that API keys can be used to create builds.

        API keys are environment-scoped, so no environment_id parameter is needed.
        NOTE: Only POST endpoints support API key auth; GET /builds requires JWT.
        """
        # Create an API key
        response = internal_authenticated_client.post(
            f"/api/v1/ui/workspaces/{test_workspace_id}"
            f"/environments/{test_environment_id}/api-keys",
            json={"name": "SDK Test Key"},
        )
        assert response.status_code == 201
        api_key = response.json()["key"]

        # Use the API key to create a build (API key implies environment)
        response = httpx.post(
            f"{docker_services.api}/api/v1/builds",
            headers={"X-API-Key": api_key},
            json={"description": "Test build via API key"},
            timeout=30.0,
        )
        # API key auth should work - the key is scoped to an environment
        assert response.status_code == 201, f"API key auth failed: {response.text}"
        data = response.json()
        assert "id" in data
        assert "environment_id" in data

    def test_api_key_auth_get_builds_works(
        self,
        internal_authenticated_client: httpx.Client,
        docker_services: ServiceEndpoints,
        test_workspace_id: str,
        test_environment_id: str,
    ) -> None:
        """Test that GET /builds supports API key auth."""
        # Create an API key
        response = internal_authenticated_client.post(
            f"/api/v1/ui/workspaces/{test_workspace_id}"
            f"/environments/{test_environment_id}/api-keys",
            json={"name": "Read Test Key"},
        )
        assert response.status_code == 201
        api_key = response.json()["key"]

        # Use the API key to GET builds - this should work
        response = httpx.get(
            f"{docker_services.api}/api/v1/builds",
            headers={"X-API-Key": api_key},
            timeout=30.0,
        )
        # API key auth works for GET /builds (environment comes from key)
        assert response.status_code == 200
        data = response.json()
        assert "builds" in data

    def test_api_key_auth_get_tasks_works(
        self,
        internal_authenticated_client: httpx.Client,
        docker_services: ServiceEndpoints,
        test_workspace_id: str,
        test_environment_id: str,
    ) -> None:
        """Test that GET /tasks supports API key auth."""
        # Create an API key
        response = internal_authenticated_client.post(
            f"/api/v1/ui/workspaces/{test_workspace_id}"
            f"/environments/{test_environment_id}/api-keys",
            json={"name": "Tasks Test Key"},
        )
        assert response.status_code == 201
        api_key = response.json()["key"]

        # Use the API key to GET tasks - this should work
        response = httpx.get(
            f"{docker_services.api}/api/v1/tasks",
            headers={"X-API-Key": api_key},
            timeout=30.0,
        )
        # API key auth works for GET /tasks (environment comes from key)
        assert response.status_code == 200
        data = response.json()
        assert "tasks" in data

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

    All SDK endpoints require authentication via API key or JWT token.
    UI bootstrap endpoints (/ui/me) accept OIDC tokens.
    """

    def test_protected_endpoints_require_auth(
        self,
        unauthenticated_client: httpx.Client,
    ) -> None:
        """Test that protected endpoints return 401 without auth."""
        endpoints = [
            "/api/v1/builds",
            "/api/v1/tasks",
            "/api/v1/ui/me",
        ]

        for endpoint in endpoints:
            response = unauthenticated_client.get(endpoint)
            assert response.status_code == 401, f"{endpoint} should require auth"

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
            ("/api/v1/tasks", 401),  # Tasks router
        ]

        for path, expected_status in endpoints_to_check:
            response = httpx.get(f"{docker_services.api}{path}", timeout=10.0)
            # 401, 405 (method not allowed), or expected status are acceptable
            assert response.status_code in (
                expected_status,
                405,
            ), f"{path} returned {response.status_code}"

    def test_wrong_workspace_returns_403(
        self,
        docker_services: ServiceEndpoints,
        internal_token: str,
    ) -> None:
        """Test that accessing resources from wrong workspace returns 403."""
        # The internal token is scoped to a specific workspace
        # Try to access an environment that doesn't exist in that workspace
        fake_environment_id = "00000000-0000-0000-0000-000000000000"
        response = httpx.get(
            f"{docker_services.api}/api/v1/builds",
            params={"environment_id": fake_environment_id},
            headers={"Authorization": f"Bearer {internal_token}"},
            timeout=30.0,
        )
        # Should be 403 (forbidden) or 404 (not found)
        assert response.status_code in (403, 404)


class TestTaskSearchApi:
    """Tests for the /tasks/search endpoint."""

    def test_search_endpoint_exists(
        self,
        internal_authenticated_client: httpx.Client,
        test_environment_id: str,
    ) -> None:
        """Test that /tasks/search endpoint exists and returns 200."""
        response = internal_authenticated_client.get(
            "/api/v1/tasks/search",
            params={"environment_id": test_environment_id},
        )
        assert response.status_code == 200
        data = response.json()
        assert "tasks" in data
        assert "total" in data

    def test_search_keys_endpoint(
        self,
        internal_authenticated_client: httpx.Client,
        test_environment_id: str,
    ) -> None:
        """Test that /tasks/search/keys endpoint returns available keys."""
        response = internal_authenticated_client.get(
            "/api/v1/tasks/search/keys",
            params={"environment_id": test_environment_id},
        )
        assert response.status_code == 200
        data = response.json()
        assert "keys" in data
        # Should include core keys
        key_names = [k["key"] for k in data["keys"]]
        assert "task_name" in key_names
        assert "status" in key_names

    def test_search_with_name_filter(
        self,
        internal_authenticated_client: httpx.Client,
        test_environment_id: str,
    ) -> None:
        """Test search with task_name filter."""
        response = internal_authenticated_client.get(
            "/api/v1/tasks/search",
            params={
                "environment_id": test_environment_id,
                "filter": "task_name:~:test",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "tasks" in data

    def test_search_with_numeric_filter(
        self,
        internal_authenticated_client: httpx.Client,
        test_environment_id: str,
    ) -> None:
        """Test search with numeric param filter (param.x:>=:10)."""
        response = internal_authenticated_client.get(
            "/api/v1/tasks/search",
            params={
                "environment_id": test_environment_id,
                "filter": "param.sample_size:>=:10",
            },
        )
        # Should return 200 (even if no results)
        assert response.status_code == 200
        data = response.json()
        assert "tasks" in data

    def test_search_with_nested_param_filter(
        self,
        internal_authenticated_client: httpx.Client,
        test_environment_id: str,
    ) -> None:
        """Test search with nested param filter (param.data.value:>:5)."""
        response = internal_authenticated_client.get(
            "/api/v1/tasks/search",
            params={
                "environment_id": test_environment_id,
                "filter": "param.data_source.sample_size:>=:10",
            },
        )
        # Should return 200 (even if no results)
        assert response.status_code == 200
        data = response.json()
        assert "tasks" in data
