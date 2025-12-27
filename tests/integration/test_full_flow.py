"""Full end-to-end flow tests.

These tests verify complete workflows across API, SDK, and UI components.
"""

import httpx

from conftest import TokenSet, exchange_oidc_for_internal_token
from docker_fixtures import ServiceEndpoints


class TestBuildWorkflow:
    """Test complete build workflow from creation to completion."""

    def test_create_build_workflow(
        self,
        internal_authenticated_client: httpx.Client,
        test_workspace_id: str,
    ) -> None:
        """Test creating a build and verifying it in the list."""
        # Create a build
        response = internal_authenticated_client.post(
            "/api/v1/builds",
            json={"description": "Integration test build"},
            params={"workspace_id": test_workspace_id},
        )
        assert response.status_code == 201
        build = response.json()
        build_id = build["id"]
        assert build["status"] == "running"

        # Verify build appears in list
        response = internal_authenticated_client.get(
            "/api/v1/builds",
            params={"workspace_id": test_workspace_id},
        )
        assert response.status_code == 200
        builds = response.json()
        # API returns {"builds": [...], "total": ..., "page": ..., "page_size": ...}
        assert "builds" in builds
        build_ids = [b["id"] for b in builds["builds"]]
        assert build_id in build_ids

        # Get build details
        response = internal_authenticated_client.get(f"/api/v1/builds/{build_id}")
        assert response.status_code == 200
        build_detail = response.json()
        assert build_detail["id"] == build_id
        assert build_detail["description"] == "Integration test build"

    def test_create_build_with_tasks_via_api_key(
        self,
        internal_authenticated_client: httpx.Client,
        docker_services: ServiceEndpoints,
        test_organization_id: str,
        test_workspace_id: str,
    ) -> None:
        """Test creating a build with tasks using API key (SDK auth).

        The task registration endpoint requires SdkAuth, so we use API key auth.
        """
        # Create an API key
        response = internal_authenticated_client.post(
            f"/api/v1/ui/organizations/{test_organization_id}"
            f"/workspaces/{test_workspace_id}/api-keys",
            json={"name": "Task Registration Key"},
        )
        assert response.status_code == 201
        api_key = response.json()["key"]

        # Create a build with API key
        response = httpx.post(
            f"{docker_services.api}/api/v1/builds",
            headers={"X-API-Key": api_key},
            json={"description": "Build with tasks"},
            timeout=30.0,
        )
        assert response.status_code == 201
        build_id = response.json()["id"]

        # Register a task using API key
        task_data = {
            "task_id": "test-task-001",
            "task_family": "TestTask",
            "task_namespace": "integration_tests",
            "task_data": {"param": "value"},
        }
        response = httpx.post(
            f"{docker_services.api}/api/v1/builds/{build_id}/tasks",
            headers={"X-API-Key": api_key},
            json=task_data,
            timeout=30.0,
        )
        assert response.status_code == 201, f"Task registration failed: {response.text}"
        task = response.json()
        assert task["task_id"] == "test-task-001"

    def test_complete_build_workflow(
        self,
        internal_authenticated_client: httpx.Client,
        docker_services: ServiceEndpoints,
        test_organization_id: str,
        test_workspace_id: str,
    ) -> None:
        """Test completing a build using API key auth."""
        # Create an API key
        response = internal_authenticated_client.post(
            f"/api/v1/ui/organizations/{test_organization_id}"
            f"/workspaces/{test_workspace_id}/api-keys",
            json={"name": "Complete Build Key"},
        )
        assert response.status_code == 201
        api_key = response.json()["key"]

        # Create a build
        response = httpx.post(
            f"{docker_services.api}/api/v1/builds",
            headers={"X-API-Key": api_key},
            json={"description": "Build to complete"},
            timeout=30.0,
        )
        assert response.status_code == 201
        build_id = response.json()["id"]
        assert response.json()["status"] == "running"

        # Complete the build (no body needed)
        response = httpx.post(
            f"{docker_services.api}/api/v1/builds/{build_id}/complete",
            headers={"X-API-Key": api_key},
            timeout=30.0,
        )
        assert response.status_code == 200, f"Complete build failed: {response.text}"

        # Verify build is completed
        response = internal_authenticated_client.get(f"/api/v1/builds/{build_id}")
        assert response.status_code == 200
        build = response.json()
        assert build["status"] == "completed"


class TestApiKeyWorkflow:
    """Test complete workflow using API key authentication."""

    def test_api_key_build_workflow(
        self,
        internal_authenticated_client: httpx.Client,
        docker_services: ServiceEndpoints,
        test_organization_id: str,
        test_workspace_id: str,
    ) -> None:
        """Test creating builds using API key auth."""
        # Create an API key
        response = internal_authenticated_client.post(
            f"/api/v1/ui/organizations/{test_organization_id}"
            f"/workspaces/{test_workspace_id}/api-keys",
            json={"name": "Workflow Test Key"},
        )
        assert response.status_code == 201
        api_key = response.json()["key"]

        # Use API key to create a build
        response = httpx.post(
            f"{docker_services.api}/api/v1/builds",
            headers={"X-API-Key": api_key},
            json={"description": "Build via API key"},
            timeout=30.0,
        )
        assert response.status_code == 201
        build = response.json()
        build_id = build["id"]
        assert build["workspace_id"] == test_workspace_id

        # Register a task using API key
        response = httpx.post(
            f"{docker_services.api}/api/v1/builds/{build_id}/tasks",
            headers={"X-API-Key": api_key},
            json={
                "task_id": "api-key-task-001",
                "task_family": "ApiKeyTask",
                "task_namespace": "api_key_tests",
                "task_data": {"key": "value"},
                "version": "1.0.0",
            },
            timeout=30.0,
        )
        assert response.status_code == 201

        # Complete build using API key
        response = httpx.post(
            f"{docker_services.api}/api/v1/builds/{build_id}/complete",
            headers={"X-API-Key": api_key},
            json={"status": "completed"},
            timeout=30.0,
        )
        assert response.status_code in (200, 204)


class TestOrganizationWorkflow:
    """Test organization and workspace management workflows."""

    def test_organization_info(
        self,
        authenticated_client: httpx.Client,
        test_organization_id: str,
    ) -> None:
        """Test retrieving organization information."""
        response = authenticated_client.get("/api/v1/ui/me")
        assert response.status_code == 200
        data = response.json()

        # User should have organization access
        assert "organizations" in data
        org_ids = [org["id"] for org in data["organizations"]]
        assert test_organization_id in org_ids

    def test_workspace_listing(
        self,
        internal_authenticated_client: httpx.Client,
        test_organization_id: str,
        test_workspace_id: str,
    ) -> None:
        """Test listing workspaces in an organization."""
        response = internal_authenticated_client.get(
            f"/api/v1/ui/organizations/{test_organization_id}/workspaces"
        )
        assert response.status_code == 200
        workspaces = response.json()

        # Test workspace should exist
        workspace_ids = [ws["id"] for ws in workspaces]
        assert test_workspace_id in workspace_ids


class TestTokenRefreshFlow:
    """Test token refresh and re-authentication flows."""

    def test_oidc_to_internal_token_exchange(
        self,
        docker_services: ServiceEndpoints,
        oidc_token: TokenSet,
        test_organization_id: str,
    ) -> None:
        """Test exchanging OIDC token for internal token."""
        internal_token = exchange_oidc_for_internal_token(
            api_url=docker_services.api,
            oidc_token=oidc_token.access_token,
            organization_id=test_organization_id,
        )
        assert internal_token is not None
        assert len(internal_token) > 0

        # Internal token should work for builds endpoint
        response = httpx.get(
            f"{docker_services.api}/api/v1/builds",
            params={
                "workspace_id": "any"
            },  # Will fail workspace check but auth should pass
            headers={"Authorization": f"Bearer {internal_token}"},
            timeout=30.0,
        )
        # Should get 404 (workspace not found) not 401 (auth failed)
        assert response.status_code in (200, 404)


class TestCrossComponentFlow:
    """Test flows that span multiple components."""

    def test_api_key_created_via_ui_works_for_sdk(
        self,
        internal_authenticated_client: httpx.Client,
        docker_services: ServiceEndpoints,
        test_organization_id: str,
        test_workspace_id: str,
    ) -> None:
        """Test that API key created via UI endpoint works for SDK operations."""
        # Create API key via UI endpoint (simulating UI creating key)
        response = internal_authenticated_client.post(
            f"/api/v1/ui/organizations/{test_organization_id}"
            f"/workspaces/{test_workspace_id}/api-keys",
            json={"name": "SDK Integration Key"},
        )
        assert response.status_code == 201
        api_key = response.json()["key"]
        key_prefix = response.json()["key_prefix"]

        # Verify key appears in list
        response = internal_authenticated_client.get(
            f"/api/v1/ui/organizations/{test_organization_id}"
            f"/workspaces/{test_workspace_id}/api-keys"
        )
        assert response.status_code == 200
        keys = response.json()
        key_prefixes = [k["key_prefix"] for k in keys]
        assert key_prefix in key_prefixes

        # Use the key for SDK operations
        response = httpx.post(
            f"{docker_services.api}/api/v1/builds",
            headers={"X-API-Key": api_key},
            json={"description": "Created with UI-generated key"},
            timeout=30.0,
        )
        assert response.status_code == 201

    def test_builds_visible_across_auth_methods(
        self,
        internal_authenticated_client: httpx.Client,
        docker_services: ServiceEndpoints,
        test_organization_id: str,
        test_workspace_id: str,
    ) -> None:
        """Test that builds are visible regardless of auth method used to create them."""
        # Create API key
        response = internal_authenticated_client.post(
            f"/api/v1/ui/organizations/{test_organization_id}"
            f"/workspaces/{test_workspace_id}/api-keys",
            json={"name": "Visibility Test Key"},
        )
        api_key = response.json()["key"]

        # Create build with API key
        response = httpx.post(
            f"{docker_services.api}/api/v1/builds",
            headers={"X-API-Key": api_key},
            json={"description": "Created with API key"},
            timeout=30.0,
        )
        assert response.status_code == 201
        api_key_build_id = response.json()["id"]

        # Create build with JWT
        response = internal_authenticated_client.post(
            "/api/v1/builds",
            json={"description": "Created with JWT"},
            params={"workspace_id": test_workspace_id},
        )
        assert response.status_code == 201
        jwt_build_id = response.json()["id"]

        # Both should be visible when listing with JWT
        response = internal_authenticated_client.get(
            "/api/v1/builds",
            params={"workspace_id": test_workspace_id},
        )
        assert response.status_code == 200
        builds = response.json()
        # API returns {"builds": [...], "total": ..., ...}
        build_ids = [b["id"] for b in builds["builds"]]

        assert api_key_build_id in build_ids
        assert jwt_build_id in build_ids


class TestSDKBuildWorkflow:
    """Test building DAGs using the stardag SDK with the API registry.

    These tests verify that the complete SDK workflow works:
    1. Define tasks using the @sd.task decorator
    2. Build a DAG using sd.build() with APIRegistry
    3. Verify tasks and build are registered in the API
    """

    def test_sdk_build_simple_dag(
        self,
        internal_authenticated_client: httpx.Client,
        docker_services: ServiceEndpoints,
        test_organization_id: str,
        test_workspace_id: str,
    ) -> None:
        """Test building a simple DAG using the SDK.

        Creates a 3-task DAG: add(1,2) -> multiply(*2) -> format
        Verifies all tasks are registered and build is completed.
        """
        import os
        import tempfile

        import stardag as sd
        from stardag.build.api_registry import APIRegistry
        from stardag.config import config_provider
        from stardag.target import target_factory_provider

        # Create an API key for this test
        response = internal_authenticated_client.post(
            f"/api/v1/ui/organizations/{test_organization_id}"
            f"/workspaces/{test_workspace_id}/api-keys",
            json={"name": "SDK Build Test Key"},
        )
        assert response.status_code == 201
        api_key = response.json()["key"]

        # Use a temporary directory for task outputs
        with tempfile.TemporaryDirectory() as tmpdir:
            # Set environment variable to configure target root
            old_env = os.environ.get("STARDAG_TARGET_ROOT__DEFAULT")
            os.environ["STARDAG_TARGET_ROOT__DEFAULT"] = tmpdir

            # Reset config and target factory to pick up new env var
            config_provider.reset()
            target_factory_provider.set(None)  # type: ignore[arg-type]

            try:

                @sd.task
                def add_numbers(a: int, b: int) -> int:
                    """Add two numbers."""
                    return a + b

                @sd.task
                def multiply_by_two(value: int) -> int:
                    """Multiply by two."""
                    return value * 2

                @sd.task
                def format_result(value: int) -> str:
                    """Format the result."""
                    return f"Result: {value}"

                # Create a simple DAG
                step1 = add_numbers(a=1, b=2)  # = 3
                step2 = multiply_by_two(value=step1)  # = 6
                final_task = format_result(value=step2)

                # Create registry with API key
                registry = APIRegistry(
                    api_url=docker_services.api,
                    api_key=api_key,
                )

                # Build the DAG
                try:
                    sd.build(final_task, registry=registry)
                finally:
                    registry.close()

                # Get the build ID
                build_id = registry.build_id
                assert build_id is not None

                # Verify build exists and is completed
                response = httpx.get(
                    f"{docker_services.api}/api/v1/builds/{build_id}",
                    headers={"X-API-Key": api_key},
                    timeout=30.0,
                )
                assert response.status_code == 200
                build = response.json()
                assert build["status"] == "completed"

                # Verify tasks were registered
                response = httpx.get(
                    f"{docker_services.api}/api/v1/builds/{build_id}/tasks",
                    headers={"X-API-Key": api_key},
                    timeout=30.0,
                )
                assert response.status_code == 200
                tasks = response.json()
                assert len(tasks) == 3

                # All tasks should be completed
                task_statuses = {t["task_family"]: t["status"] for t in tasks}
                assert task_statuses.get("add_numbers") == "completed"
                assert task_statuses.get("multiply_by_two") == "completed"
                assert task_statuses.get("format_result") == "completed"

            finally:
                # Restore environment
                if old_env is not None:
                    os.environ["STARDAG_TARGET_ROOT__DEFAULT"] = old_env
                else:
                    os.environ.pop("STARDAG_TARGET_ROOT__DEFAULT", None)
                # Reset providers again
                config_provider.reset()
                target_factory_provider.set(None)  # type: ignore[arg-type]

    def test_sdk_build_with_diamond_dag(
        self,
        internal_authenticated_client: httpx.Client,
        docker_services: ServiceEndpoints,
        test_organization_id: str,
        test_workspace_id: str,
    ) -> None:
        r"""Test building a diamond-shaped DAG using the SDK.

        Diamond pattern:
            start
           /     \
          left   right
           \     /
            merge

        This tests that the SDK correctly handles shared dependencies.
        """
        import os
        import tempfile

        import stardag as sd
        from stardag.build.api_registry import APIRegistry
        from stardag.config import config_provider
        from stardag.target import target_factory_provider

        # Create an API key for this test
        response = internal_authenticated_client.post(
            f"/api/v1/ui/organizations/{test_organization_id}"
            f"/workspaces/{test_workspace_id}/api-keys",
            json={"name": "SDK Diamond DAG Test Key"},
        )
        assert response.status_code == 201
        api_key = response.json()["key"]

        with tempfile.TemporaryDirectory() as tmpdir:
            # Set environment variable to configure target root
            old_env = os.environ.get("STARDAG_TARGET_ROOT__DEFAULT")
            os.environ["STARDAG_TARGET_ROOT__DEFAULT"] = tmpdir

            # Reset config and target factory to pick up new env var
            config_provider.reset()
            target_factory_provider.set(None)  # type: ignore[arg-type]

            try:

                @sd.task
                def start_value(x: int) -> int:
                    """Starting value."""
                    return x

                @sd.task
                def left_branch(value: int) -> int:
                    """Left branch: multiply by 2."""
                    return value * 2

                @sd.task
                def right_branch(value: int) -> int:
                    """Right branch: add 10."""
                    return value + 10

                @sd.task
                def merge_branches(left: int, right: int) -> int:
                    """Merge: add both branches."""
                    return left + right

                # Create diamond DAG
                start = start_value(x=5)  # = 5
                left = left_branch(value=start)  # = 10
                right = right_branch(value=start)  # = 15
                final_task = merge_branches(left=left, right=right)  # = 25

                # Create registry with API key
                registry = APIRegistry(
                    api_url=docker_services.api,
                    api_key=api_key,
                )

                # Build the DAG
                try:
                    sd.build(final_task, registry=registry)
                finally:
                    registry.close()

                # Get the build ID
                build_id = registry.build_id
                assert build_id is not None

                # Verify build is completed
                response = httpx.get(
                    f"{docker_services.api}/api/v1/builds/{build_id}",
                    headers={"X-API-Key": api_key},
                    timeout=30.0,
                )
                assert response.status_code == 200
                build = response.json()
                assert build["status"] == "completed"

                # Verify all 4 tasks were registered
                response = httpx.get(
                    f"{docker_services.api}/api/v1/builds/{build_id}/tasks",
                    headers={"X-API-Key": api_key},
                    timeout=30.0,
                )
                assert response.status_code == 200
                tasks = response.json()
                assert len(tasks) == 4

                # All tasks should be completed
                for task in tasks:
                    assert (
                        task["status"] == "completed"
                    ), f"{task['task_family']} not completed"

                # Verify the task graph structure
                response = httpx.get(
                    f"{docker_services.api}/api/v1/builds/{build_id}/graph",
                    headers={"X-API-Key": api_key},
                    timeout=30.0,
                )
                assert response.status_code == 200
                graph = response.json()

                # Should have 4 nodes and 4 edges
                # (start->left, start->right, left->merge, right->merge)
                assert len(graph["nodes"]) == 4
                assert len(graph["edges"]) == 4

            finally:
                # Restore environment
                if old_env is not None:
                    os.environ["STARDAG_TARGET_ROOT__DEFAULT"] = old_env
                else:
                    os.environ.pop("STARDAG_TARGET_ROOT__DEFAULT", None)
                # Reset providers again
                config_provider.reset()
                target_factory_provider.set(None)  # type: ignore[arg-type]
