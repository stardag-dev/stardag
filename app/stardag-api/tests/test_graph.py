"""Tests for graph traversal endpoints (recursive upstream traversal & grouping)."""

import pytest
from httpx import AsyncClient


# --- Helpers ---


async def create_build(client: AsyncClient) -> str:
    response = await client.post("/api/v1/builds", json={})
    assert response.status_code == 201
    return response.json()["id"]


async def register_task(
    client: AsyncClient,
    build_id: str,
    task_id: str,
    task_name: str,
    dependency_task_ids: list[str] | None = None,
    task_namespace: str = "test",
) -> dict:
    data = {
        "task_id": task_id,
        "task_namespace": task_namespace,
        "task_name": task_name,
        "task_data": {"name": task_name},
        "dependency_task_ids": dependency_task_ids or [],
    }
    response = await client.post(f"/api/v1/builds/{build_id}/tasks", json=data)
    assert response.status_code == 201
    return response.json()


# --- Build Graph Tests ---


@pytest.mark.asyncio
async def test_build_graph_basic(client: AsyncClient):
    """Basic graph (no upstream traversal) — returns extended-shape response.

    The endpoint always uses the traversal/grouping pipeline so that
    ``max_per_type_per_level`` applies uniformly regardless of depth.
    With only a handful of tasks, ``groups`` is empty.
    """
    build_id = await create_build(client)
    await register_task(client, build_id, "t-a", "TaskA")
    await register_task(client, build_id, "t-b", "TaskB", dependency_task_ids=["t-a"])

    response = await client.get(f"/api/v1/builds/{build_id}/graph")
    assert response.status_code == 200
    data = response.json()
    assert len(data["nodes"]) == 2
    assert len(data["edges"]) == 1
    # Extended response is always returned; groups is empty when nothing
    # exceeds the grouping threshold.
    assert data.get("groups", []) == []


@pytest.mark.asyncio
async def test_build_graph_upstream_depth_zero(client: AsyncClient):
    """upstream_depth=0 returns the build's own tasks (no cross-build traversal)."""
    build_id = await create_build(client)
    await register_task(client, build_id, "t-a", "TaskA")

    response = await client.get(f"/api/v1/builds/{build_id}/graph?upstream_depth=0")
    assert response.status_code == 200
    data = response.json()
    assert len(data["nodes"]) == 1
    assert data.get("groups", []) == []


@pytest.mark.asyncio
async def test_build_graph_groups_at_depth_zero(client: AsyncClient):
    """Grouping applies at depth=0 — many structurally-identical tasks collapse.

    This is the motivating case: within a single build, having e.g. 6
    ``LoadChunk`` tasks should collapse to a single batch node when
    ``max_per_type_per_level`` is smaller than that count, regardless of
    whether cross-build traversal is requested.
    """
    build_id = await create_build(client)
    for i in range(6):
        await register_task(client, build_id, f"dz-load-{i}", "LoadChunk")

    response = await client.get(
        f"/api/v1/builds/{build_id}/graph?upstream_depth=0&max_per_type_per_level=3"
    )
    assert response.status_code == 200
    data = response.json()
    # 6 LoadChunks exceed max_per_type_per_level=3 → collapse into one group
    assert len(data["nodes"]) == 0
    assert len(data["groups"]) == 1
    group = data["groups"][0]
    assert group["task_name"] == "LoadChunk"
    assert group["count"] == 6


@pytest.mark.asyncio
async def test_build_graph_upstream_depth_one(client: AsyncClient):
    """Test upstream traversal across builds."""
    # Build 1: register upstream task
    build1_id = await create_build(client)
    await register_task(client, build1_id, "upstream-1", "UpstreamTask")

    # Build 2: register downstream that depends on upstream
    build2_id = await create_build(client)
    await register_task(
        client,
        build2_id,
        "downstream-1",
        "DownstreamTask",
        dependency_task_ids=["upstream-1"],
    )

    # Get graph for build2 with upstream_depth=1
    response = await client.get(f"/api/v1/builds/{build2_id}/graph?upstream_depth=1")
    assert response.status_code == 200
    data = response.json()

    # Should have extended response fields
    assert "groups" in data
    assert "truncated" in data
    assert "total_upstream_count" in data

    # Should find both tasks
    node_names = {n["task_name"] for n in data["nodes"]}
    assert "DownstreamTask" in node_names
    assert "UpstreamTask" in node_names

    # Downstream should be primary, upstream should not
    for node in data["nodes"]:
        if node["task_name"] == "DownstreamTask":
            assert node["is_primary"] is True
            assert node["traversal_depth"] == 0
        elif node["task_name"] == "UpstreamTask":
            assert node["is_primary"] is False
            assert node["traversal_depth"] == 1


@pytest.mark.asyncio
async def test_build_graph_depth_limiting(client: AsyncClient):
    """Test that traversal stops at max depth."""
    build_id = await create_build(client)

    # Create a chain: A -> B -> C -> D (3 levels deep)
    await register_task(client, build_id, "t-a", "TaskA")
    await register_task(client, build_id, "t-b", "TaskB", dependency_task_ids=["t-a"])
    await register_task(client, build_id, "t-c", "TaskC", dependency_task_ids=["t-b"])
    await register_task(client, build_id, "t-d", "TaskD", dependency_task_ids=["t-c"])

    # Build 2: only has TaskD
    build2_id = await create_build(client)
    await register_task(client, build2_id, "t-d", "TaskD", dependency_task_ids=["t-c"])

    # Depth 1: should find TaskD (primary) + TaskC (upstream)
    response = await client.get(f"/api/v1/builds/{build2_id}/graph?upstream_depth=1")
    data = response.json()
    node_names = {n["task_name"] for n in data["nodes"]}
    assert "TaskD" in node_names
    assert "TaskC" in node_names
    assert "TaskB" not in node_names

    # Depth 3: should find all 4 tasks
    response = await client.get(f"/api/v1/builds/{build2_id}/graph?upstream_depth=3")
    data = response.json()
    node_names = {n["task_name"] for n in data["nodes"]}
    assert node_names == {"TaskA", "TaskB", "TaskC", "TaskD"}
    assert data["total_upstream_count"] == 3


@pytest.mark.asyncio
async def test_build_graph_grouping(client: AsyncClient):
    """Test that same-type tasks exceeding max_per_type_per_level get grouped."""
    build1_id = await create_build(client)

    # Create 5 upstream tasks of the same type
    for i in range(5):
        await register_task(client, build1_id, f"data-{i}", "LoadData")

    # Downstream depends on all of them
    build2_id = await create_build(client)
    await register_task(
        client,
        build2_id,
        "aggregate",
        "Aggregate",
        dependency_task_ids=[f"data-{i}" for i in range(5)],
    )

    # With max_per_type=2, all 5 LoadData tasks should be grouped (all-or-nothing)
    response = await client.get(
        f"/api/v1/builds/{build2_id}/graph?upstream_depth=1&max_per_type_per_level=2"
    )
    data = response.json()

    assert len(data["groups"]) == 1
    group = data["groups"][0]
    assert group["task_name"] == "LoadData"
    assert group["count"] == 5
    assert group["depth"] == 1
    assert group["status"] == "pending"  # no events = pending
    assert len(group["sample_task_ids"]) == 5  # up to 5 samples

    # No individual LoadData nodes should exist (all-or-nothing)
    node_names = {n["task_name"] for n in data["nodes"]}
    assert "LoadData" not in node_names
    assert "Aggregate" in node_names


@pytest.mark.asyncio
async def test_build_graph_max_total_nodes(client: AsyncClient):
    """Test that max_total_nodes truncates the result."""
    build_id = await create_build(client)

    # Create 10 tasks
    for i in range(10):
        await register_task(client, build_id, f"task-{i}", f"Task{i}")

    response = await client.get(
        f"/api/v1/builds/{build_id}/graph?upstream_depth=1&max_total_nodes=5"
    )
    data = response.json()
    assert len(data["nodes"]) <= 5
    assert data["truncated"] is True


@pytest.mark.asyncio
async def test_build_graph_edges_between_traversed(client: AsyncClient):
    """Test that edges are included between upstream tasks."""
    build_id = await create_build(client)

    # Chain: A -> B -> C
    await register_task(client, build_id, "t-a", "TaskA")
    await register_task(client, build_id, "t-b", "TaskB", dependency_task_ids=["t-a"])
    await register_task(client, build_id, "t-c", "TaskC", dependency_task_ids=["t-b"])

    response = await client.get(f"/api/v1/builds/{build_id}/graph?upstream_depth=2")
    data = response.json()

    # Should have edges A->B and B->C
    edge_pairs = {(e["source"], e["target"]) for e in data["edges"]}
    node_id_by_name = {n["task_name"]: n["id"] for n in data["nodes"]}

    assert (node_id_by_name["TaskA"], node_id_by_name["TaskB"]) in edge_pairs
    assert (node_id_by_name["TaskB"], node_id_by_name["TaskC"]) in edge_pairs


# --- Task Graph Endpoint Tests ---


@pytest.mark.asyncio
async def test_task_graph_basic(client: AsyncClient):
    """Test the /tasks/graph endpoint with no upstream traversal."""
    build_id = await create_build(client)
    await register_task(client, build_id, "t-x", "TaskX")
    await register_task(client, build_id, "t-y", "TaskY", dependency_task_ids=["t-x"])

    response = await client.post(
        "/api/v1/tasks/graph", json={"task_ids": ["t-x", "t-y"]}
    )
    assert response.status_code == 200
    data = response.json()

    assert len(data["nodes"]) == 2
    assert "groups" in data  # Always extended response
    node_names = {n["task_name"] for n in data["nodes"]}
    assert node_names == {"TaskX", "TaskY"}


@pytest.mark.asyncio
async def test_task_graph_cross_build(client: AsyncClient):
    """Test /tasks/graph resolves tasks across multiple builds."""
    build1_id = await create_build(client)
    await register_task(client, build1_id, "shared-task", "SharedTask")

    build2_id = await create_build(client)
    await register_task(
        client,
        build2_id,
        "consumer",
        "Consumer",
        dependency_task_ids=["shared-task"],
    )

    # Query with both task IDs (from different builds)
    response = await client.post(
        "/api/v1/tasks/graph",
        json={"task_ids": ["shared-task", "consumer"], "upstream_depth": 1},
    )
    data = response.json()

    node_names = {n["task_name"] for n in data["nodes"]}
    assert "SharedTask" in node_names
    assert "Consumer" in node_names


@pytest.mark.asyncio
async def test_task_graph_empty_task_ids(client: AsyncClient):
    """Test /tasks/graph with empty task_ids returns empty response."""
    response = await client.post("/api/v1/tasks/graph", json={"task_ids": []})
    assert response.status_code == 200
    data = response.json()
    assert data["nodes"] == []
    assert data["edges"] == []


@pytest.mark.asyncio
async def test_task_graph_nonexistent_task_ids(client: AsyncClient):
    """Test /tasks/graph with nonexistent task_ids returns empty response."""
    response = await client.post(
        "/api/v1/tasks/graph",
        json={"task_ids": ["nonexistent-1", "nonexistent-2"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["nodes"] == []


@pytest.mark.asyncio
async def test_task_graph_upstream_traversal(client: AsyncClient):
    """Test /tasks/graph with upstream traversal."""
    build_id = await create_build(client)

    # Diamond: A,B -> C -> D
    await register_task(client, build_id, "t-a", "TaskA")
    await register_task(client, build_id, "t-b", "TaskB")
    await register_task(
        client,
        build_id,
        "t-c",
        "TaskC",
        dependency_task_ids=["t-a", "t-b"],
    )
    await register_task(
        client,
        build_id,
        "t-d",
        "TaskD",
        dependency_task_ids=["t-c"],
    )

    # Query only t-d, traverse upstream 2 levels
    response = await client.post(
        "/api/v1/tasks/graph",
        json={"task_ids": ["t-d"], "upstream_depth": 2},
    )
    data = response.json()

    node_names = {n["task_name"] for n in data["nodes"]}
    assert "TaskD" in node_names
    assert "TaskC" in node_names
    assert "TaskA" in node_names
    assert "TaskB" in node_names

    # t-d is primary, rest are not
    for node in data["nodes"]:
        if node["task_name"] == "TaskD":
            assert node["is_primary"] is True
        else:
            assert node["is_primary"] is False


@pytest.mark.asyncio
async def test_diamond_graph_no_duplicates(client: AsyncClient):
    """Test that diamond dependency patterns don't create duplicate nodes."""
    build_id = await create_build(client)

    # Diamond: A -> B, A -> C, B -> D, C -> D
    await register_task(client, build_id, "t-a", "TaskA")
    await register_task(client, build_id, "t-b", "TaskB", dependency_task_ids=["t-a"])
    await register_task(client, build_id, "t-c", "TaskC", dependency_task_ids=["t-a"])
    await register_task(
        client,
        build_id,
        "t-d",
        "TaskD",
        dependency_task_ids=["t-b", "t-c"],
    )

    response = await client.get(f"/api/v1/builds/{build_id}/graph?upstream_depth=3")
    data = response.json()

    # Should have exactly 4 nodes (no duplicates)
    assert len(data["nodes"]) == 4
    task_ids = [n["task_id"] for n in data["nodes"]]
    assert len(set(task_ids)) == 4


# --- is_dynamic propagation through group collapsing ---


@pytest.mark.asyncio
async def test_graph_group_edge_propagates_is_dynamic(client: AsyncClient):
    """When underlying edges are dynamic, the collapsed group edge is marked dynamic.

    Several same-type upstream tasks get collapsed into a group. Some of their
    edges to the downstream are dynamic (recorded via
    ``POST /.../dependencies``); the synthesized group → downstream edge in
    the extended graph should have ``is_dynamic=True``.
    """
    build1_id = await create_build(client)

    # Create 5 upstream tasks of the same type
    for i in range(5):
        await register_task(client, build1_id, f"pg-data-{i}", "LoadData")

    # Downstream registered with only static deps from 2 of them
    build2_id = await create_build(client)
    await register_task(
        client,
        build2_id,
        "pg-aggregate",
        "Aggregate",
        dependency_task_ids=["pg-data-0", "pg-data-1"],
    )

    # Add dynamic deps for the remaining 3 via the runtime endpoint
    response = await client.post(
        f"/api/v1/builds/{build2_id}/tasks/pg-aggregate/dependencies",
        json={
            "upstream_task_ids": ["pg-data-2", "pg-data-3", "pg-data-4"],
            "is_dynamic": True,
        },
    )
    assert response.status_code == 200

    # Query extended graph with grouping so all 5 LoadData tasks collapse
    response = await client.post(
        "/api/v1/tasks/graph",
        json={
            "task_ids": ["pg-aggregate"],
            "upstream_depth": 1,
            "downstream_depth": 0,
            "max_per_type_per_level": 2,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()

    # LoadData tasks collapsed into one group
    assert len(data["groups"]) == 1
    group_id = data["groups"][0]["group_id"]

    # The group → aggregate edge should be marked dynamic (at least one
    # contributor is dynamic)
    agg_node = next(n for n in data["nodes"] if n["task_id"] == "pg-aggregate")
    agg_pk = agg_node["id"]
    group_edges = [e for e in data["edges"] if e["source"] == group_id]
    assert len(group_edges) == 1
    assert group_edges[0]["target"] == agg_pk
    assert group_edges[0]["is_dynamic"] is True


@pytest.mark.asyncio
async def test_graph_group_edge_static_only(client: AsyncClient):
    """All-static contributors → group edge stays is_dynamic=False."""
    build1_id = await create_build(client)
    for i in range(4):
        await register_task(client, build1_id, f"ps-data-{i}", "LoadData")

    build2_id = await create_build(client)
    await register_task(
        client,
        build2_id,
        "ps-aggregate",
        "Aggregate",
        dependency_task_ids=[f"ps-data-{i}" for i in range(4)],
    )

    response = await client.post(
        "/api/v1/tasks/graph",
        json={
            "task_ids": ["ps-aggregate"],
            "upstream_depth": 1,
            "downstream_depth": 0,
            "max_per_type_per_level": 2,
        },
    )
    data = response.json()

    group_id = data["groups"][0]["group_id"]
    group_edges = [e for e in data["edges"] if e["source"] == group_id]
    assert len(group_edges) == 1
    assert group_edges[0]["is_dynamic"] is False
