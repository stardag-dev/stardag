"""Full end-to-end flow tests.

These tests verify complete workflows across API, SDK, and UI components.
"""

from pathlib import Path

import httpx

from stardag_integration_tests.conftest import (
    TokenSet,
    exchange_oidc_for_internal_token,
)
from stardag_integration_tests.docker_fixtures import ServiceEndpoints


class TestBuildWorkflow:
    """Test complete build workflow from creation to completion."""

    def test_create_build_workflow(
        self,
        internal_authenticated_client: httpx.Client,
        test_environment_id: str,
    ) -> None:
        """Test creating a build and verifying it in the list.

        Bare build creation (no plan) is still a raw-HTTP concern in v2:
        ``root_task_ids`` just names the request at completion-id level, no
        plan required yet.
        """
        # Create a build
        response = internal_authenticated_client.post(
            "/api/v2/builds",
            json={
                "description": "Integration test build",
                "root_task_ids": ["placeholder-root"],
            },
            params={"environment_id": test_environment_id},
        )
        assert response.status_code == 200
        build = response.json()
        build_id = build["id"]
        assert build["status"] == "running"

        # Verify build appears in list
        response = internal_authenticated_client.get(
            "/api/v2/builds",
            params={"environment_id": test_environment_id},
        )
        assert response.status_code == 200
        builds = response.json()
        assert "builds" in builds
        build_ids = [b["id"] for b in builds["builds"]]
        assert build_id in build_ids

        # Get build details
        response = internal_authenticated_client.get(
            f"/api/v2/builds/{build_id}",
            params={"environment_id": test_environment_id},
        )
        assert response.status_code == 200
        build_detail = response.json()
        assert build_detail["id"] == build_id
        assert build_detail["description"] == "Integration test build"

    def test_create_build_with_tasks_via_api_key(
        self,
        docker_services: ServiceEndpoints,
        sdk_api_key: str,
        temporary_default_target_root: Path,
    ) -> None:
        """Test creating a build with a real task using API key (SDK auth).

        v2 ties task registration to the plan protocol (design.md,
        "Registration"): there is no ad-hoc ``POST /builds/{id}/tasks`` left,
        so "creating a build with tasks" means running the real SDK build
        flow, exactly as an SDK user would.
        """
        import uuid

        import stardag as sd
        from stardag.registry import APIRegistry

        @sd.task
        def task_registration_demo(value: str) -> str:
            return value

        # A fresh value per run: this test environment is long-lived, and a
        # fixed value would collide (409 task_identity_conflict) with an
        # earlier run's output_uri for the same completion.
        task = task_registration_demo(value=str(uuid.uuid4()))
        registry = APIRegistry(api_url=docker_services.api, api_key=sdk_api_key)
        try:
            build_summary = sd.build_sequential([task], registry=registry)
        finally:
            registry.close()

        response = httpx.get(
            f"{docker_services.api}/api/v2/tasks/{task.id}",
            headers={"X-API-Key": sdk_api_key},
            timeout=30.0,
        )
        assert response.status_code == 200, f"Task registration failed: {response.text}"
        registered = response.json()
        assert registered["task_id"] == str(task.id)
        assert registered["task_name"] == "task_registration_demo"
        assert registered["status"] == "completed"
        assert build_summary.build_id is not None

    def test_complete_build_workflow(
        self,
        internal_authenticated_client: httpx.Client,
        docker_services: ServiceEndpoints,
        sdk_api_key: str,
        test_environment_id: str,
        temporary_default_target_root: Path,
    ) -> None:
        """Test completing a build using API key auth.

        ``POST /builds/{id}/complete`` now refuses (409 ``plan_incomplete``)
        unless the active plan is sealed and every member is COMPLETED
        (services/builds.py, ``_verify_plan_complete``), so a build can no
        longer be completed bare -- it has to actually run something,
        exactly as ``sd.build_sequential`` does.
        """
        import uuid

        import stardag as sd
        from stardag.registry import APIRegistry

        @sd.task
        def build_to_complete(value: str) -> str:
            return value

        task = build_to_complete(value=str(uuid.uuid4()))
        registry = APIRegistry(api_url=docker_services.api, api_key=sdk_api_key)
        try:
            build_summary = sd.build_sequential([task], registry=registry)
        finally:
            registry.close()

        # Verify build is completed
        response = internal_authenticated_client.get(
            f"/api/v2/builds/{build_summary.build_id}",
            params={"environment_id": test_environment_id},
        )
        assert response.status_code == 200
        build = response.json()
        assert build["status"] == "completed"


class TestApiKeyWorkflow:
    """Test complete workflow using API key authentication."""

    def test_api_key_build_workflow(
        self,
        docker_services: ServiceEndpoints,
        sdk_api_key: str,
        temporary_default_target_root: Path,
    ) -> None:
        """Test the full build-a-task-and-complete workflow using API key
        auth, via the real SDK flow (see ``TestBuildWorkflow`` for why: v2
        has no ad-hoc task-registration or bare-complete route left)."""
        import uuid

        import stardag as sd
        from stardag.registry import APIRegistry

        @sd.task
        def api_key_workflow_task(value: str) -> str:
            return value

        task = api_key_workflow_task(value=str(uuid.uuid4()))
        registry = APIRegistry(api_url=docker_services.api, api_key=sdk_api_key)
        try:
            build_summary = sd.build_sequential([task], registry=registry)
        finally:
            registry.close()

        response = httpx.get(
            f"{docker_services.api}/api/v2/builds/{build_summary.build_id}",
            headers={"X-API-Key": sdk_api_key},
            timeout=30.0,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "completed"


class TestWorkspaceWorkflow:
    """Test workspace and environment management workflows."""

    def test_workspace_info(
        self,
        authenticated_client: httpx.Client,
        test_workspace_id: str,
    ) -> None:
        """Test retrieving workspace information."""
        response = authenticated_client.get("/api/v1/ui/me")
        assert response.status_code == 200
        data = response.json()

        # User should have workspace access
        assert "workspaces" in data
        workspace_ids = [ws["id"] for ws in data["workspaces"]]
        assert test_workspace_id in workspace_ids

    def test_environment_listing(
        self,
        internal_authenticated_client: httpx.Client,
        test_workspace_id: str,
        test_environment_id: str,
    ) -> None:
        """Test listing environments in a workspace."""
        response = internal_authenticated_client.get(
            f"/api/v1/ui/workspaces/{test_workspace_id}/environments"
        )
        assert response.status_code == 200
        environments = response.json()

        # Test environment should exist
        environment_ids = [ws["id"] for ws in environments]
        assert test_environment_id in environment_ids


class TestTokenRefreshFlow:
    """Test token refresh and re-authentication flows."""

    def test_oidc_to_internal_token_exchange(
        self,
        docker_services: ServiceEndpoints,
        oidc_token: TokenSet,
        test_workspace_id: str,
    ) -> None:
        """Test exchanging OIDC token for internal token."""
        internal_token = exchange_oidc_for_internal_token(
            api_url=docker_services.api,
            oidc_token=oidc_token.access_token,
            workspace_id=test_workspace_id,
        )
        assert internal_token is not None
        assert len(internal_token) > 0

        # Internal token should work for builds endpoint
        response = httpx.get(
            f"{docker_services.api}/api/v2/builds",
            params={
                "environment_id": "any"
            },  # Will fail environment check but auth should pass
            headers={"Authorization": f"Bearer {internal_token}"},
            timeout=30.0,
        )
        # Should get 404 (environment not found) not 401 (auth failed)
        assert response.status_code in (200, 404)


class TestCrossComponentFlow:
    """Test flows that span multiple components."""

    def test_api_key_created_via_ui_works_for_sdk(
        self,
        internal_authenticated_client: httpx.Client,
        docker_services: ServiceEndpoints,
        test_workspace_id: str,
        test_environment_id: str,
    ) -> None:
        """Test that API key created via UI endpoint works for SDK operations."""
        # Create API key via UI endpoint (simulating UI creating key)
        response = internal_authenticated_client.post(
            f"/api/v1/ui/workspaces/{test_workspace_id}"
            f"/environments/{test_environment_id}/api-keys",
            json={"name": "SDK Integration Key"},
        )
        assert response.status_code == 201
        api_key = response.json()["key"]
        key_prefix = response.json()["key_prefix"]

        # Verify key appears in list
        response = internal_authenticated_client.get(
            f"/api/v1/ui/workspaces/{test_workspace_id}"
            f"/environments/{test_environment_id}/api-keys"
        )
        assert response.status_code == 200
        keys = response.json()
        key_prefixes = [k["key_prefix"] for k in keys]
        assert key_prefix in key_prefixes

        # Use the key for SDK operations
        response = httpx.post(
            f"{docker_services.api}/api/v2/builds",
            headers={"X-API-Key": api_key},
            json={
                "description": "Created with UI-generated key",
                "root_task_ids": ["placeholder-root"],
            },
            timeout=30.0,
        )
        assert response.status_code == 200

    def test_builds_visible_across_auth_methods(
        self,
        internal_authenticated_client: httpx.Client,
        docker_services: ServiceEndpoints,
        test_workspace_id: str,
        test_environment_id: str,
    ) -> None:
        """Test that builds are visible regardless of auth method used to create them."""
        # Create API key
        response = internal_authenticated_client.post(
            f"/api/v1/ui/workspaces/{test_workspace_id}"
            f"/environments/{test_environment_id}/api-keys",
            json={"name": "Visibility Test Key"},
        )
        api_key = response.json()["key"]

        # Create build with API key
        response = httpx.post(
            f"{docker_services.api}/api/v2/builds",
            headers={"X-API-Key": api_key},
            json={
                "description": "Created with API key",
                "root_task_ids": ["placeholder-root"],
            },
            timeout=30.0,
        )
        assert response.status_code == 200
        api_key_build_id = response.json()["id"]

        # Create build with JWT
        response = internal_authenticated_client.post(
            "/api/v2/builds",
            json={
                "description": "Created with JWT",
                "root_task_ids": ["placeholder-root"],
            },
            params={"environment_id": test_environment_id},
        )
        assert response.status_code == 200
        jwt_build_id = response.json()["id"]

        # Both should be visible when listing with JWT
        response = internal_authenticated_client.get(
            "/api/v2/builds",
            params={"environment_id": test_environment_id},
        )
        assert response.status_code == 200
        builds = response.json()
        build_ids = [b["id"] for b in builds["builds"]]

        assert api_key_build_id in build_ids
        assert jwt_build_id in build_ids


def _build_task_with_artifacts(
    docker_services: ServiceEndpoints,
    api_key: str,
    label: str,
) -> str:
    """Build one task whose ``artifacts()`` returns a markdown and a JSON
    artifact, via the real SDK flow. Artifacts belong to the promise
    (design.md, "Peripheral tables, re-pointed") and are uploaded by the
    build session itself through ``POST
    /plans/{plan_id}/members/{task_id}/artifacts`` once the task
    completes -- there is no ad-hoc "upload an artifact for this task_id"
    route independent of a real build. Returns the task id.

    ``label`` is combined with a fresh uuid4 into the task's only
    significant field, so every call gets its own task id -- this test
    environment is long-lived, and a fixed value would collide (409
    ``task_identity_conflict``) with an earlier run's output_uri for the
    same completion.
    """
    import uuid

    import stardag as sd
    from stardag.artifact import Artifact, JSONArtifact, MarkdownArtifact
    from stardag.registry import APIRegistry

    @sd.task
    def artifact_producer(marker: str) -> str:
        return marker

    def _artifacts(self: object) -> list[Artifact]:
        return [
            MarkdownArtifact(
                name="report", body="# Test Report\n\nThis is a test report."
            ),
            JSONArtifact(name="metrics", body={"accuracy": 0.95, "loss": 0.05}),
        ]

    artifact_producer.artifacts = _artifacts  # type: ignore[attr-defined]

    task = artifact_producer(marker=f"{label}-{uuid.uuid4()}")
    registry = APIRegistry(api_url=docker_services.api, api_key=api_key)
    try:
        sd.build_sequential([task], registry=registry)
    finally:
        registry.close()
    return str(task.id)


class TestTaskArtifactsWorkflow:
    """Test task artifacts workflow.

    These tests verify:
    1. Uploading artifacts via API key (SDK flow)
    2. Fetching artifacts via JWT (UI flow) - requires environment_id
    3. Artifact visibility across auth methods
    """

    def test_upload_and_fetch_artifacts_via_api_key(
        self,
        docker_services: ServiceEndpoints,
        sdk_api_key: str,
        temporary_default_target_root: Path,
    ) -> None:
        """Test uploading and fetching artifacts using API key."""
        task_id = _build_task_with_artifacts(
            docker_services, sdk_api_key, label="upload-and-fetch"
        )

        # Fetch artifacts using API key
        response = httpx.get(
            f"{docker_services.api}/api/v2/tasks/{task_id}/artifacts",
            headers={"X-API-Key": sdk_api_key},
            timeout=30.0,
        )
        assert response.status_code == 200
        fetched = response.json()
        assert len(fetched["artifacts"]) == 2

        # Verify artifact content
        artifact_by_name = {a["name"]: a for a in fetched["artifacts"]}
        assert artifact_by_name["report"]["artifact_type"] == "markdown"
        assert artifact_by_name["report"]["body"]["content"].startswith("# Test Report")
        assert artifact_by_name["metrics"]["artifact_type"] == "json"
        assert artifact_by_name["metrics"]["body"]["accuracy"] == 0.95

    def test_fetch_artifacts_via_jwt_requires_environment_id(
        self,
        docker_services: ServiceEndpoints,
        sdk_api_key: str,
        test_environment_id: str,
        internal_token: str,
        temporary_default_target_root: Path,
    ) -> None:
        """Test that fetching artifacts with JWT requires environment_id.

        This is the critical test for the UI flow - JWT auth requires
        environment_id to be passed as a query parameter.
        """
        task_id = _build_task_with_artifacts(
            docker_services, sdk_api_key, label="jwt-requires-environment-id"
        )

        # Try to fetch artifacts with JWT but WITHOUT environment_id - should fail
        response = httpx.get(
            f"{docker_services.api}/api/v2/tasks/{task_id}/artifacts",
            headers={"Authorization": f"Bearer {internal_token}"},
            timeout=30.0,
        )
        assert response.status_code == 400, (
            f"Expected 400 when environment_id is missing with JWT, got {response.status_code}"
        )
        assert "environment_id" in response.text.lower()

        # Fetch with environment_id - should succeed
        response = httpx.get(
            f"{docker_services.api}/api/v2/tasks/{task_id}/artifacts",
            headers={"Authorization": f"Bearer {internal_token}"},
            params={"environment_id": test_environment_id},
            timeout=30.0,
        )
        assert response.status_code == 200, (
            f"Failed with environment_id: {response.text}"
        )
        fetched = response.json()
        assert len(fetched["artifacts"]) == 2
        names = {a["name"] for a in fetched["artifacts"]}
        assert names == {"report", "metrics"}

    def test_artifacts_visible_across_auth_methods(
        self,
        docker_services: ServiceEndpoints,
        sdk_api_key: str,
        test_environment_id: str,
        internal_token: str,
        temporary_default_target_root: Path,
    ) -> None:
        """Test that artifacts uploaded via API key are visible via JWT."""
        task_id = _build_task_with_artifacts(
            docker_services, sdk_api_key, label="cross-auth-visibility"
        )

        # Verify visible via JWT (with environment_id)
        response = httpx.get(
            f"{docker_services.api}/api/v2/tasks/{task_id}/artifacts",
            headers={"Authorization": f"Bearer {internal_token}"},
            params={"environment_id": test_environment_id},
            timeout=30.0,
        )
        assert response.status_code == 200
        artifacts = response.json()["artifacts"]
        assert len(artifacts) == 2
        metrics = next(a for a in artifacts if a["name"] == "metrics")
        assert metrics["body"]["accuracy"] == 0.95


class TestSDKBuildWorkflow:
    """Test building DAGs using the stardag SDK with the API registry.

    These tests verify that the complete SDK workflow works:
    1. Define tasks using the @sd.task decorator
    2. Build a DAG using sd.build() with APIRegistry
    3. Verify tasks and build are registered in the API
    """

    def test_sdk_build_simple_dag(
        self,
        docker_services: ServiceEndpoints,
        sdk_api_key: str,
        temporary_default_target_root: Path,
    ) -> None:
        """Test building a simple DAG using the SDK.

        Creates a 3-task DAG: add(1,2) -> multiply(*2) -> format
        Verifies all tasks are registered and build is completed.
        """

        import uuid

        import stardag as sd
        from stardag.registry import APIRegistry

        @sd.task
        def add_numbers(a: int, b: int, run_id: str) -> int:
            """Add two numbers. ``run_id`` is otherwise unused: it just
            gives the root task a fresh id per test run, so a re-run
            against this long-lived test environment doesn't collide
            (409 ``task_identity_conflict``) with an earlier run's
            output_uri for the same (a, b)."""
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
        step1 = add_numbers(a=1, b=2, run_id=str(uuid.uuid4()))  # = 3
        step2 = multiply_by_two(value=step1)  # = 6
        final_task = format_result(value=step2)

        # Create registry with API key
        registry = APIRegistry(
            api_url=docker_services.api,
            api_key=sdk_api_key,
        )

        # Build the DAG (use build_sequential to avoid event loop conflict
        # with Playwright's async runtime)
        try:
            build_summary = sd.build_sequential([final_task], registry=registry)
        finally:
            registry.close()

        # Get the build ID from the summary
        build_id = build_summary.build_id
        assert build_id is not None

        # Verify build exists and is completed
        response = httpx.get(
            f"{docker_services.api}/api/v2/builds/{build_id}",
            headers={"X-API-Key": sdk_api_key},
            timeout=30.0,
        )
        assert response.status_code == 200
        build = response.json()
        assert build["status"] == "completed"

        # v2 has no "list tasks of a build" route (registration is
        # plan-scoped, not build-scoped -- design.md, "Registration"), so
        # each task is checked individually by its own task id.
        for task, expected_name in (
            (step1, "add_numbers"),
            (step2, "multiply_by_two"),
            (final_task, "format_result"),
        ):
            response = httpx.get(
                f"{docker_services.api}/api/v2/tasks/{task.id}",
                headers={"X-API-Key": sdk_api_key},
                timeout=30.0,
            )
            assert response.status_code == 200
            task_data = response.json()
            assert task_data["task_name"] == expected_name
            assert task_data["status"] == "completed"

    def test_sdk_build_with_diamond_dag(
        self,
        docker_services: ServiceEndpoints,
        sdk_api_key: str,
        temporary_default_target_root: Path,
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

        import uuid

        import stardag as sd
        from stardag.registry import APIRegistry

        @sd.task
        def start_value(x: int, run_id: str) -> int:
            """Starting value. ``run_id`` is otherwise unused: see
            ``add_numbers`` in ``test_sdk_build_simple_dag`` for why the
            root of the DAG needs a fresh id per test run."""
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
        start = start_value(x=5, run_id=str(uuid.uuid4()))  # = 5
        left = left_branch(value=start)  # = 10
        right = right_branch(value=start)  # = 15
        final_task = merge_branches(left=left, right=right)  # = 25

        # Create registry with API key
        registry = APIRegistry(
            api_url=docker_services.api,
            api_key=sdk_api_key,
        )

        # Build the DAG (use build_sequential to avoid event loop conflict
        # with Playwright's async runtime)
        try:
            build_summary = sd.build_sequential([final_task], registry=registry)
        finally:
            registry.close()

        # Get the build ID from the summary
        build_id = build_summary.build_id
        assert build_id is not None

        # Verify build is completed
        response = httpx.get(
            f"{docker_services.api}/api/v2/builds/{build_id}",
            headers={"X-API-Key": sdk_api_key},
            timeout=30.0,
        )
        assert response.status_code == 200
        build = response.json()
        assert build["status"] == "completed"

        # Verify all 4 tasks were registered and completed. v2 has no
        # "list tasks of a build" route, so each is checked by its own
        # task id (see test_sdk_build_simple_dag).
        for task, expected_name in (
            (start, "start_value"),
            (left, "left_branch"),
            (right, "right_branch"),
            (final_task, "merge_branches"),
        ):
            response = httpx.get(
                f"{docker_services.api}/api/v2/tasks/{task.id}",
                headers={"X-API-Key": sdk_api_key},
                timeout=30.0,
            )
            assert response.status_code == 200
            task_data = response.json()
            assert task_data["task_name"] == expected_name
            assert task_data["status"] == "completed"

        # The task graph structure (nodes/edges) has no v2 read route yet
        # -- plan.md's status section lists "the graph over instance
        # edges as a read route" among what I0 skipped, owned by I4 --
        # so the shared-dependency shape can no longer be asserted
        # end-to-end here. The 4 completed tasks above, reached only
        # because the SDK walked the diamond and registered each once,
        # already exercise the shared-dependency path; a dedicated
        # graph-shape assertion belongs with that route once it exists.
