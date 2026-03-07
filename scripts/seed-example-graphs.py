#!/usr/bin/env python3
"""Seed the local docker-compose database with example DAG graphs.

Usage:
    # From the stardag/ directory (requires httpx):
    uv run --with httpx python scripts/seed-example-graphs.py

Creates several builds with different graph topologies for manual testing
of the DAG view, upstream traversal, and grouping features.

Auth flow: Keycloak password grant -> exchange for internal token -> use as Bearer.
"""

import asyncio
import os
import sys

import httpx

API_URL = os.environ.get("API_URL", "http://localhost:8000")
API_BASE = f"{API_URL}/api/v1"
KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "http://localhost:8080")

# Keycloak test user (from docker/keycloak/realm-export.json)
TEST_USER = "testuser"
TEST_PASSWORD = "testpassword"


def get_oidc_token() -> str:
    """Get OIDC access token via Keycloak password grant."""
    token_url = f"{KEYCLOAK_URL}/realms/stardag/protocol/openid-connect/token"
    resp = httpx.post(
        token_url,
        data={
            "grant_type": "password",
            "client_id": "stardag-test",
            "username": TEST_USER,
            "password": TEST_PASSWORD,
            "scope": "openid profile email",
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Keycloak token request failed: {resp.status_code} - {resp.text}"
        )
    return resp.json()["access_token"]


def get_workspace_and_environment(oidc_token: str) -> tuple[str, str]:
    """Get workspace ID and environment ID, creating environment if needed."""
    headers = {"Authorization": f"Bearer {oidc_token}"}
    resp = httpx.get(f"{API_BASE}/ui/me", headers=headers, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()

    workspaces = data.get("workspaces", [])
    if not workspaces:
        raise RuntimeError(
            "No workspaces found for test user. Log in to the UI first to create one."
        )

    workspace = workspaces[0]
    workspace_id = workspace["id"]

    # Fetch environments separately (not included in /ui/me)
    internal_token = exchange_for_internal_token(oidc_token, workspace_id)
    int_headers = {"Authorization": f"Bearer {internal_token}"}
    resp = httpx.get(
        f"{API_BASE}/ui/workspaces/{workspace_id}/environments",
        headers=int_headers,
        timeout=30.0,
    )
    resp.raise_for_status()
    environments = resp.json()

    if environments:
        environment_id = environments[0]["id"]
        return workspace_id, environment_id

    # No environment exists — create one
    print("No environments found, creating one...")
    resp = httpx.post(
        f"{API_BASE}/ui/workspaces/{workspace_id}/environments",
        json={"name": "Default", "slug": "default"},
        headers=int_headers,
        timeout=30.0,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Failed to create environment: {resp.status_code} - {resp.text}"
        )
    environment_id = resp.json()["id"]
    print(f"Created environment: {environment_id}")
    return workspace_id, environment_id


def exchange_for_internal_token(oidc_token: str, workspace_id: str) -> str:
    """Exchange OIDC token for workspace-scoped internal token."""
    resp = httpx.post(
        f"{API_BASE}/auth/exchange",
        json={"workspace_id": workspace_id},
        headers={"Authorization": f"Bearer {oidc_token}"},
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Token exchange failed: {resp.status_code} - {resp.text}")
    return resp.json()["access_token"]


# --- API helpers (use authenticated client) ---


async def create_build(
    client: httpx.AsyncClient, env_id: str, description: str = ""
) -> str:
    resp = await client.post(
        f"{API_BASE}/builds",
        json={"description": description},
        params={"environment_id": env_id},
    )
    resp.raise_for_status()
    build_id = resp.json()["id"]
    print(f"  Created build: {build_id} ({description})")
    return build_id


async def register_task(
    client: httpx.AsyncClient,
    build_id: str,
    env_id: str,
    task_id: str,
    task_name: str,
    dependency_task_ids: list[str] | None = None,
    task_namespace: str = "",
    task_data: dict | None = None,
) -> dict:
    data = {
        "task_id": task_id,
        "task_namespace": task_namespace,
        "task_name": task_name,
        "task_data": task_data or {"name": task_name, "task_id": task_id},
        "dependency_task_ids": dependency_task_ids or [],
    }
    resp = await client.post(
        f"{API_BASE}/builds/{build_id}/tasks",
        json=data,
        params={"environment_id": env_id},
    )
    resp.raise_for_status()
    return resp.json()


async def start_task(
    client: httpx.AsyncClient, build_id: str, task_id: str, env_id: str
):
    resp = await client.post(
        f"{API_BASE}/builds/{build_id}/tasks/{task_id}/start",
        params={"environment_id": env_id},
    )
    resp.raise_for_status()


async def complete_task(
    client: httpx.AsyncClient, build_id: str, task_id: str, env_id: str
):
    resp = await client.post(
        f"{API_BASE}/builds/{build_id}/tasks/{task_id}/complete",
        params={"environment_id": env_id},
    )
    resp.raise_for_status()


async def complete_build(client: httpx.AsyncClient, build_id: str, env_id: str):
    resp = await client.post(
        f"{API_BASE}/builds/{build_id}/complete",
        params={"environment_id": env_id},
    )
    resp.raise_for_status()


# --- Scenarios ---


async def seed_linear_chain(client: httpx.AsyncClient, env_id: str):
    """Linear chain: Extract -> Transform -> Load -> Report (4 tasks)."""
    print("\n=== Scenario 1: Linear ETL Pipeline ===")
    bid = await create_build(client, env_id, "Linear ETL Pipeline")

    await register_task(
        client, bid, env_id, "extract-data", "ExtractData", task_namespace="etl"
    )
    await register_task(
        client,
        bid,
        env_id,
        "transform-data",
        "TransformData",
        dependency_task_ids=["extract-data"],
        task_namespace="etl",
    )
    await register_task(
        client,
        bid,
        env_id,
        "load-data",
        "LoadData",
        dependency_task_ids=["transform-data"],
        task_namespace="etl",
    )
    await register_task(
        client,
        bid,
        env_id,
        "generate-report",
        "GenerateReport",
        dependency_task_ids=["load-data"],
        task_namespace="etl",
    )

    await start_task(client, bid, "extract-data", env_id)
    await complete_task(client, bid, "extract-data", env_id)
    await start_task(client, bid, "transform-data", env_id)
    await complete_task(client, bid, "transform-data", env_id)
    await start_task(client, bid, "load-data", env_id)

    print("  -> 4 tasks, linear chain, load-data running")


async def seed_diamond_dag(client: httpx.AsyncClient, env_id: str):
    """Diamond: DataSource -> [FeatureA, FeatureB, FeatureC] -> TrainModel -> Evaluate."""
    print("\n=== Scenario 2: ML Pipeline (Diamond DAG) ===")
    bid = await create_build(client, env_id, "ML Training Pipeline")

    await register_task(
        client, bid, env_id, "data-source", "LoadDataSource", task_namespace="ml"
    )
    await register_task(
        client,
        bid,
        env_id,
        "feature-a",
        "ComputeFeatureA",
        dependency_task_ids=["data-source"],
        task_namespace="ml",
    )
    await register_task(
        client,
        bid,
        env_id,
        "feature-b",
        "ComputeFeatureB",
        dependency_task_ids=["data-source"],
        task_namespace="ml",
    )
    await register_task(
        client,
        bid,
        env_id,
        "feature-c",
        "ComputeFeatureC",
        dependency_task_ids=["data-source"],
        task_namespace="ml",
    )
    await register_task(
        client,
        bid,
        env_id,
        "train-model",
        "TrainModel",
        dependency_task_ids=["feature-a", "feature-b", "feature-c"],
        task_namespace="ml",
    )
    await register_task(
        client,
        bid,
        env_id,
        "evaluate",
        "EvaluateModel",
        dependency_task_ids=["train-model"],
        task_namespace="ml",
    )

    await start_task(client, bid, "data-source", env_id)
    await complete_task(client, bid, "data-source", env_id)
    await start_task(client, bid, "feature-a", env_id)
    await complete_task(client, bid, "feature-a", env_id)
    await start_task(client, bid, "feature-b", env_id)
    await complete_task(client, bid, "feature-b", env_id)
    await start_task(client, bid, "feature-c", env_id)

    print("  -> 6 tasks, diamond DAG, feature-c running")


async def seed_cross_build_deps(client: httpx.AsyncClient, env_id: str):
    """Two builds sharing upstream tasks (cross-build upstream traversal)."""
    print("\n=== Scenario 3: Cross-Build Dependencies ===")

    b1 = await create_build(client, env_id, "Data Pipeline (upstream)")

    await register_task(
        client, b1, env_id, "ingest-raw", "IngestRawData", task_namespace="data"
    )
    await register_task(
        client,
        b1,
        env_id,
        "clean-data",
        "CleanData",
        dependency_task_ids=["ingest-raw"],
        task_namespace="data",
    )
    await register_task(
        client,
        b1,
        env_id,
        "feature-store",
        "UpdateFeatureStore",
        dependency_task_ids=["clean-data"],
        task_namespace="data",
    )

    await start_task(client, b1, "ingest-raw", env_id)
    await complete_task(client, b1, "ingest-raw", env_id)
    await start_task(client, b1, "clean-data", env_id)
    await complete_task(client, b1, "clean-data", env_id)
    await start_task(client, b1, "feature-store", env_id)
    await complete_task(client, b1, "feature-store", env_id)
    await complete_build(client, b1, env_id)

    print("  Build 1: Data pipeline, all completed")

    b2 = await create_build(client, env_id, "ML Pipeline V2 (downstream)")

    await register_task(
        client,
        b2,
        env_id,
        "train-v2",
        "TrainModelV2",
        dependency_task_ids=["feature-store"],
        task_namespace="ml",
    )
    await register_task(
        client,
        b2,
        env_id,
        "evaluate-v2",
        "EvaluateModelV2",
        dependency_task_ids=["train-v2"],
        task_namespace="ml",
    )
    await register_task(
        client,
        b2,
        env_id,
        "deploy-model",
        "DeployModel",
        dependency_task_ids=["evaluate-v2"],
        task_namespace="ml",
    )

    await start_task(client, b2, "train-v2", env_id)
    await complete_task(client, b2, "train-v2", env_id)
    await start_task(client, b2, "evaluate-v2", env_id)

    print("  Build 2: ML pipeline, evaluate-v2 running")
    print("  -> Use upstream_depth=1 on build 2 to see feature-store from build 1")
    print("  -> Use upstream_depth=3 to see full chain back to ingest-raw")


async def seed_wide_fan_in(client: httpx.AsyncClient, env_id: str):
    """Wide fan-in: 20 DataLoaders -> Aggregate -> Summary (grouping test)."""
    print("\n=== Scenario 4: Wide Fan-In (for batch grouping) ===")

    b1 = await create_build(client, env_id, "Data Loading (20 loaders)")

    loader_ids = []
    for i in range(20):
        tid = f"load-partition-{i:02d}"
        loader_ids.append(tid)
        await register_task(
            client,
            b1,
            env_id,
            tid,
            "LoadPartition",
            task_namespace="data",
            task_data={"partition": i, "source": f"s3://data/part-{i:02d}.parquet"},
        )
        await start_task(client, b1, tid, env_id)
        await complete_task(client, b1, tid, env_id)

    await complete_build(client, b1, env_id)
    print("  Build 1: 20 LoadPartition tasks, all completed")

    b2 = await create_build(client, env_id, "Aggregation Pipeline")

    await register_task(
        client,
        b2,
        env_id,
        "aggregate-all",
        "AggregatePartitions",
        dependency_task_ids=loader_ids,
        task_namespace="data",
    )
    await register_task(
        client,
        b2,
        env_id,
        "generate-summary",
        "GenerateSummary",
        dependency_task_ids=["aggregate-all"],
        task_namespace="data",
    )

    await start_task(client, b2, "aggregate-all", env_id)

    print("  Build 2: Aggregate + Summary")
    print("  -> upstream_depth=1: shows all 20 loaders")
    print("  -> upstream_depth=1&max_per_type_per_level=5: groups 15 into batch node")


async def seed_deep_chain(client: httpx.AsyncClient, env_id: str):
    """Deep chain across 4 builds (multi-level upstream traversal)."""
    print("\n=== Scenario 5: Deep Chain Across 4 Builds ===")

    b1 = await create_build(client, env_id, "Deep Chain - Stage 1")
    await register_task(
        client, b1, env_id, "raw-data", "RawData", task_namespace="pipeline"
    )
    await start_task(client, b1, "raw-data", env_id)
    await complete_task(client, b1, "raw-data", env_id)
    await complete_build(client, b1, env_id)
    print("  Build 1: RawData")

    b2 = await create_build(client, env_id, "Deep Chain - Stage 2")
    await register_task(
        client,
        b2,
        env_id,
        "step-1",
        "ProcessStep1",
        dependency_task_ids=["raw-data"],
        task_namespace="pipeline",
    )
    await register_task(
        client,
        b2,
        env_id,
        "step-2",
        "ProcessStep2",
        dependency_task_ids=["step-1"],
        task_namespace="pipeline",
    )
    await start_task(client, b2, "step-1", env_id)
    await complete_task(client, b2, "step-1", env_id)
    await start_task(client, b2, "step-2", env_id)
    await complete_task(client, b2, "step-2", env_id)
    await complete_build(client, b2, env_id)
    print("  Build 2: ProcessStep1 -> ProcessStep2")

    b3 = await create_build(client, env_id, "Deep Chain - Stage 3")
    await register_task(
        client,
        b3,
        env_id,
        "step-3",
        "ProcessStep3",
        dependency_task_ids=["step-2"],
        task_namespace="pipeline",
    )
    await register_task(
        client,
        b3,
        env_id,
        "step-4",
        "ProcessStep4",
        dependency_task_ids=["step-3"],
        task_namespace="pipeline",
    )
    await start_task(client, b3, "step-3", env_id)
    await complete_task(client, b3, "step-3", env_id)
    await start_task(client, b3, "step-4", env_id)
    await complete_task(client, b3, "step-4", env_id)
    await complete_build(client, b3, env_id)
    print("  Build 3: ProcessStep3 -> ProcessStep4")

    b4 = await create_build(client, env_id, "Deep Chain - Stage 4 (final)")
    await register_task(
        client,
        b4,
        env_id,
        "final-output",
        "FinalOutput",
        dependency_task_ids=["step-4"],
        task_namespace="pipeline",
    )
    await start_task(client, b4, "final-output", env_id)
    print("  Build 4: FinalOutput (running)")

    print("  -> Open build 4, increase upstream depth to see chain:")
    print("     depth=1: FinalOutput + ProcessStep4")
    print("     depth=2: + ProcessStep3")
    print("     depth=4: all 5 tasks across 4 builds")


async def main():
    print("Seeding example graphs into local database...")
    print(f"API: {API_URL}")
    print(f"Keycloak: {KEYCLOAK_URL}")

    # 1. Check API is reachable
    try:
        resp = httpx.get(f"{API_URL}/health", timeout=10.0)
        resp.raise_for_status()
        print("API is healthy.")
    except (httpx.ConnectError, httpx.HTTPStatusError) as e:
        print(f"\nError: Cannot reach API at {API_URL}")
        print(f"  {e}")
        print("\nMake sure docker-compose is running: docker-compose up -d")
        sys.exit(1)

    # 2. Get OIDC token from Keycloak
    print("Authenticating with Keycloak...")
    try:
        oidc_token = get_oidc_token()
    except RuntimeError as e:
        print(f"\nError: {e}")
        print("Make sure Keycloak is running and the test user exists.")
        sys.exit(1)

    # 3. Get workspace and environment
    print("Getting workspace and environment...")
    try:
        workspace_id, environment_id = get_workspace_and_environment(oidc_token)
    except RuntimeError as e:
        print(f"\nError: {e}")
        print("Log in to http://localhost:3000 first to create a workspace.")
        sys.exit(1)

    print(f"  Workspace: {workspace_id}")
    print(f"  Environment: {environment_id}")

    # 4. Exchange for internal (workspace-scoped) token
    print("Exchanging for internal token...")
    internal_token = exchange_for_internal_token(oidc_token, workspace_id)

    # 5. Seed data
    async with httpx.AsyncClient(
        timeout=30.0,
        headers={"Authorization": f"Bearer {internal_token}"},
    ) as client:
        await seed_linear_chain(client, environment_id)
        await seed_diamond_dag(client, environment_id)
        await seed_cross_build_deps(client, environment_id)
        await seed_wide_fan_in(client, environment_id)
        await seed_deep_chain(client, environment_id)

    print("\n" + "=" * 60)
    print("Done! Seeded 5 scenarios (11 builds total).")
    print("=" * 60)
    print()
    print("Open http://localhost:3000 and log in as:")
    print(f"  Username: {TEST_USER}")
    print(f"  Password: {TEST_PASSWORD}")
    print()
    print("What to try:")
    print("  1. Go to Builds, open any build -> DAG view is shown by default")
    print("  2. Use the 'Upstream depth' slider in the DAG header to reveal")
    print("     upstream dependencies from other builds")
    print()
    print("Best scenarios for upstream traversal:")
    print("  - 'ML Pipeline V2 (downstream)': depth=1 shows feature-store,")
    print("    depth=3 shows full chain back to IngestRawData")
    print("  - 'Deep Chain - Stage 4 (final)': depth=1..4 reveals progressive chain")
    print("  - 'Aggregation Pipeline': depth=1 shows 20 LoadPartition tasks,")
    print("    try the Task Explorer with upstream_depth to see batch grouping")


if __name__ == "__main__":
    asyncio.run(main())
