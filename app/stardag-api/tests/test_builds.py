"""Tests for build management endpoints."""

import pytest
from httpx import AsyncClient

from tests.conftest import DEFAULT_ENVIRONMENT_ID_STR


@pytest.mark.asyncio
async def test_create_build(client: AsyncClient):
    """Test creating a new run."""
    build_data = {
        "environment_id": DEFAULT_ENVIRONMENT_ID_STR,
        "user": "default",
        "commit_hash": "abc123",
        "root_task_ids": [],
        "description": "Test build",
    }
    response = await client.post("/api/v1/builds", json=build_data)
    assert response.status_code == 201
    data = response.json()
    assert data["environment_id"] == DEFAULT_ENVIRONMENT_ID_STR
    assert data["status"] == "running"  # Build starts in buildning state
    assert data["name"] is not None  # Has memorable slug
    assert "-" in data["name"]  # Format: adjective-noun-number


@pytest.mark.asyncio
async def test_create_build_minimal(client: AsyncClient):
    """Test creating a build with minimal data (defaults)."""
    response = await client.post("/api/v1/builds", json={})
    assert response.status_code == 201
    data = response.json()
    assert data["environment_id"] == DEFAULT_ENVIRONMENT_ID_STR
    assert data["status"] == "running"


@pytest.mark.asyncio
async def test_get_build(client: AsyncClient):
    """Test retrieving a build by ID."""
    # Create a build first
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Get it back
    response = await client.get(f"/api/v1/builds/{build_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == build_id
    assert data["status"] == "running"


@pytest.mark.asyncio
async def test_get_build_not_found(client: AsyncClient):
    """Test that getting a non-existent run returns 404."""
    # Use a valid UUID format that doesn't exist in the database
    fake_uuid = "00000000-0000-0000-0000-000000000099"
    response = await client.get(f"/api/v1/builds/{fake_uuid}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_builds(client: AsyncClient):
    """Test listing runs with pagination."""
    # Create multiple runs
    for _ in range(3):
        await client.post("/api/v1/builds", json={})

    response = await client.get(
        "/api/v1/builds",
        params={"environment_id": DEFAULT_ENVIRONMENT_ID_STR, "page_size": 2},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["builds"]) == 2
    assert data["total"] == 3
    assert data["page"] == 1
    assert data["page_size"] == 2


@pytest.mark.asyncio
async def test_complete_build(client: AsyncClient):
    """Test completing a build."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Complete it
    response = await client.post(f"/api/v1/builds/{build_id}/complete")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["completed_at"] is not None


@pytest.mark.asyncio
async def test_fail_build(client: AsyncClient):
    """Test failing a build with error message."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Fail it
    response = await client.post(
        f"/api/v1/builds/{build_id}/fail", params={"error_message": "Something broke"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "failed"
    assert data["completed_at"] is not None


@pytest.mark.asyncio
async def test_resume_build_after_failure(client: AsyncClient):
    """Resuming a failed build flips its status back to running.

    Reproduces the bug we fixed: previously, sd.build(resume_build_id=...)
    silently reused the build_id but never told the registry — so a build
    that had previously emitted BUILD_FAILED kept showing as failed in the
    UI even though the SDK was actively running tasks under it again.
    """
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Build fails
    await client.post(f"/api/v1/builds/{build_id}/fail")
    response = await client.get(f"/api/v1/builds/{build_id}")
    assert response.json()["status"] == "failed"
    assert response.json()["is_resumed"] is False

    # Now resume — this is what the SDK fires on sd.build(resume_build_id=...)
    response = await client.post(f"/api/v1/builds/{build_id}/resume")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "running"
    assert data["completed_at"] is None  # cleared by BUILD_RESUMED replay
    assert data["is_resumed"] is True


@pytest.mark.asyncio
async def test_resume_fresh_build_is_noop(client: AsyncClient):
    """Resuming a build with no activity beyond BUILD_STARTED records nothing.

    This is the trigger-minted-build-id flow: the client creates the build,
    then the first orchestrator invocation attaches to it via the resume
    endpoint (the SDK calls resume before task discovery/registration). The
    first run must not show as resumed.
    """
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    response = await client.post(f"/api/v1/builds/{build_id}/resume")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "running"
    assert data["is_resumed"] is False

    # No BUILD_RESUMED event was recorded
    response = await client.get(f"/api/v1/builds/{build_id}/events")
    event_types = [e["event_type"] for e in response.json()]
    assert "build_resumed" not in event_types
    assert "build_started" in event_types


@pytest.mark.asyncio
async def test_resume_build_with_task_activity_records_resumed(client: AsyncClient):
    """Resuming a build that has task activity records BUILD_RESUMED.

    E.g. the orchestrator was restarted mid-build (after registering/starting
    tasks): the second invocation's resume call must flag the build as
    resumed even though the build never reached a terminal state.
    """
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    task_data = {
        "task_id": "resume-activity-task",
        "task_namespace": "",
        "task_name": "TestTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    await client.post(f"/api/v1/builds/{build_id}/tasks/resume-activity-task/start")

    response = await client.post(f"/api/v1/builds/{build_id}/resume")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "running"
    assert data["is_resumed"] is True

    response = await client.get(f"/api/v1/builds/{build_id}/events")
    event_types = [e["event_type"] for e in response.json()]
    assert "build_resumed" in event_types
    assert event_types.count("build_resumed") == 1


@pytest.mark.asyncio
async def test_resume_build_complete_clears_resumed_flag(client: AsyncClient):
    """A resumed build that subsequently completes shows is_resumed=False.

    Once a terminal event lands after BUILD_RESUMED, the resumed-flag
    semantic is gone — get_build_status only flags is_resumed when the
    most recent build-level event is BUILD_RESUMED.
    """
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    await client.post(f"/api/v1/builds/{build_id}/fail")
    await client.post(f"/api/v1/builds/{build_id}/resume")
    await client.post(f"/api/v1/builds/{build_id}/complete")

    response = await client.get(f"/api/v1/builds/{build_id}")
    data = response.json()
    assert data["status"] == "completed"
    assert data["is_resumed"] is False


@pytest.mark.asyncio
async def test_resume_build_not_found(client: AsyncClient):
    """Resume on a non-existent build returns 404 with a resource-level body.

    The SDK's missing-route fallback (APIRegistry.build_resume_aio)
    distinguishes FastAPI's default ``{"detail": "Not Found"}`` (route
    doesn't exist on this server) from app-level 404s like the one this
    test exercises. Pinning the response body here keeps that contract
    explicit on the API side.
    """
    fake_uuid = "00000000-0000-0000-0000-000000000099"
    response = await client.post(f"/api/v1/builds/{fake_uuid}/resume")
    assert response.status_code == 404
    assert response.json()["detail"] == "Build not found"


@pytest.mark.asyncio
async def test_list_builds_orders_resumed_first(client: AsyncClient):
    """A resumed build jumps to the top of the list (sorted by last_active_at).

    Without this, the resumed build would stay buried at its original
    created_at position — the whole UX point of the fix.

    A small ``asyncio.sleep`` between creates guarantees distinct
    ``last_active_at`` timestamps even on coarse-resolution CI clocks
    where back-to-back ``utc_now()`` calls can collide. The pre-resume
    ordering assertion uses a set membership check so it doesn't depend
    on ``Build.id.desc()`` (the UUID7 tiebreaker) for builds that did
    happen to tie.
    """
    import asyncio

    build_a = (await client.post("/api/v1/builds", json={})).json()["id"]
    # Give build_a activity so its later resume is a real resume (a fresh
    # build's resume is a recorded-nothing no-op and wouldn't reorder).
    await client.post(f"/api/v1/builds/{build_a}/fail")
    await asyncio.sleep(0.005)
    build_b = (await client.post("/api/v1/builds", json={})).json()["id"]
    await asyncio.sleep(0.005)
    build_c = (await client.post("/api/v1/builds", json={})).json()["id"]

    response = await client.get(
        "/api/v1/builds", params={"environment_id": DEFAULT_ENVIRONMENT_ID_STR}
    )
    ids = [b["id"] for b in response.json()["builds"]]
    assert set(ids[:3]) == {build_a, build_b, build_c}
    # Newest first when timestamps are distinct.
    assert ids[:3] == [build_c, build_b, build_a]

    # Resume the oldest — it should jump to the top.
    await asyncio.sleep(0.005)
    await client.post(f"/api/v1/builds/{build_a}/resume")

    response = await client.get(
        "/api/v1/builds", params={"environment_id": DEFAULT_ENVIRONMENT_ID_STR}
    )
    ids = [b["id"] for b in response.json()["builds"]]
    assert ids[0] == build_a, f"Expected resumed build at top, got {ids}"


@pytest.mark.asyncio
async def test_register_task_to_build(client: AsyncClient):
    """Test registering a task within a build."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register a task
    task_data = {
        "task_id": "test-task-123",
        "task_namespace": "test",
        "task_name": "TestTask",
        "task_data": {"param": "value"},
        "dependency_task_ids": [],
    }
    response = await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    assert response.status_code == 201
    data = response.json()
    assert data["task_id"] == "test-task-123"
    assert data["task_name"] == "TestTask"


@pytest.mark.asyncio
async def test_start_task_in_build(client: AsyncClient):
    """Test starting a task within a build."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register a task
    task_data = {
        "task_id": "start-task-123",
        "task_namespace": "",
        "task_name": "TestTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)

    # Start the task
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/start-task-123/start"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == "start-task-123"
    assert data["status"] == "running"


@pytest.mark.asyncio
async def test_complete_task_in_build(client: AsyncClient):
    """Test completing a task within a build."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register and start a task
    task_data = {
        "task_id": "complete-task-123",
        "task_namespace": "",
        "task_name": "TestTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    await client.post(f"/api/v1/builds/{build_id}/tasks/complete-task-123/start")

    # Complete the task
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/complete-task-123/complete"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == "complete-task-123"
    assert data["status"] == "completed"


@pytest.mark.asyncio
async def test_fail_task_in_build(client: AsyncClient):
    """Test failing a task within a build."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register and start a task
    task_data = {
        "task_id": "fail-task-123",
        "task_namespace": "",
        "task_name": "TestTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    await client.post(f"/api/v1/builds/{build_id}/tasks/fail-task-123/start")

    # Fail the task
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/fail-task-123/fail",
        params={"error_message": "Task error"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == "fail-task-123"
    assert data["status"] == "failed"


@pytest.mark.asyncio
async def test_skip_task_in_build(client: AsyncClient):
    """Test skipping a task whose dependency failed."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    task_data = {
        "task_id": "skip-task-123",
        "task_namespace": "",
        "task_name": "TestTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)

    response = await client.post(f"/api/v1/builds/{build_id}/tasks/skip-task-123/skip")
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == "skip-task-123"
    assert data["status"] == "skipped"


@pytest.mark.asyncio
async def test_skip_unknown_task_returns_404(client: AsyncClient):
    """Test that skipping an unregistered task returns 404."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/never-registered/skip"
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_tasks_in_build(client: AsyncClient):
    """Test listing all tasks in a build."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register multiple tasks
    for i in range(3):
        task_data = {
            "task_id": f"list-task-{i}",
            "task_namespace": "",
            "task_name": "TestTask",
            "task_data": {},
        }
        await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)

    # List tasks in build
    response = await client.get(f"/api/v1/builds/{build_id}/tasks")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3


@pytest.mark.asyncio
async def test_list_events_in_build(client: AsyncClient):
    """Test listing events for a build."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register and start a task to generate events
    task_data = {
        "task_id": "event-task",
        "task_namespace": "",
        "task_name": "TestTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    await client.post(f"/api/v1/builds/{build_id}/tasks/event-task/start")

    # List events
    response = await client.get(f"/api/v1/builds/{build_id}/events")
    assert response.status_code == 200
    data = response.json()
    # Should have: RUN_STARTED, TASK_PENDING, TASK_STARTED
    assert len(data) >= 3


@pytest.mark.asyncio
async def test_get_build_graph(client: AsyncClient):
    """Test getting the task graph for a build."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register tasks with dependency
    await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json={
            "task_id": "upstream-task",
            "task_namespace": "",
            "task_name": "UpstreamTask",
            "task_data": {},
        },
    )
    await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json={
            "task_id": "downstream-task",
            "task_namespace": "",
            "task_name": "DownstreamTask",
            "task_data": {},
            "dependency_task_ids": ["upstream-task"],
        },
    )

    # Get graph
    response = await client.get(f"/api/v1/builds/{build_id}/graph")
    assert response.status_code == 200
    data = response.json()
    assert len(data["nodes"]) == 2
    assert len(data["edges"]) == 1


@pytest.mark.asyncio
async def test_list_tasks_in_build_returns_stable_order(client: AsyncClient):
    """Tasks are returned in per-build registration order.

    The SDK registers every discovered task during the discovery walk in
    post-order — static deps first, then their parents. The UI relies on
    this endpoint ordering by *first event in this build* (not by
    ``Task.created_at``, which is the global "first ever seen in this
    environment" timestamp). Without that, repeated calls would return
    rows in arbitrary order.
    """
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    expected_order = [f"order-task-{i}" for i in range(5)]
    for task_id in expected_order:
        await client.post(
            f"/api/v1/builds/{build_id}/tasks",
            json={
                "task_id": task_id,
                "task_namespace": "",
                "task_name": task_id,
                "task_data": {},
            },
        )

    response = await client.get(f"/api/v1/builds/{build_id}/tasks")
    assert response.status_code == 200
    actual_order = [t["task_id"] for t in response.json()]
    assert actual_order == expected_order, (
        f"Expected stable insertion order; got {actual_order}"
    )

    # Hitting the endpoint a second time must return the same order.
    response2 = await client.get(f"/api/v1/builds/{build_id}/tasks")
    assert [t["task_id"] for t in response2.json()] == expected_order


@pytest.mark.asyncio
async def test_bulk_register_creates_tasks_and_resolves_in_batch_deps(
    client: AsyncClient,
):
    """Bulk register processes the array in order so that a task whose deps
    appear earlier in the same batch resolves to existing rows — no
    phantom-creation in _reconcile_dependency_edges. This is the key
    invariant the SDK's post-order discover relies on.
    """
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Leaf, then mid, then root. Root depends on mid, mid depends on leaf.
    payload = {
        "tasks": [
            {
                "task_id": "bulk-leaf",
                "task_namespace": "",
                "task_name": "Leaf",
                "task_data": {},
            },
            {
                "task_id": "bulk-mid",
                "task_namespace": "",
                "task_name": "Mid",
                "task_data": {},
                "dependency_task_ids": ["bulk-leaf"],
            },
            {
                "task_id": "bulk-root",
                "task_namespace": "",
                "task_name": "Root",
                "task_data": {},
                "dependency_task_ids": ["bulk-mid"],
            },
        ]
    }
    response = await client.post(f"/api/v1/builds/{build_id}/tasks/bulk", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert [t["task_id"] for t in data["tasks"]] == [
        "bulk-leaf",
        "bulk-mid",
        "bulk-root",
    ]
    # No phantom rows in the response — every task got registered with full
    # data, never as a placeholder.
    assert all(t["is_phantom"] is False for t in data["tasks"])

    # Sanity: list endpoint returns all three with proper namespaces and
    # names (proves no phantoms persist after the bulk call).
    response = await client.get(f"/api/v1/builds/{build_id}/tasks")
    assert response.status_code == 200
    listed = response.json()
    assert len(listed) == 3
    assert all(t["task_name"] in {"Leaf", "Mid", "Root"} for t in listed)
    assert all(t["is_phantom"] is False for t in listed)


@pytest.mark.asyncio
async def test_bulk_register_upgrades_existing_phantoms(client: AsyncClient):
    """A phantom row created by an earlier dep-edge call gets upgraded in
    place by a later bulk register that sends real task_data.

    Phantoms don't appear in ``list_tasks_in_build`` (the endpoint filters
    by tasks with events in the build, and phantoms are event-less), so
    we use the graph endpoint — which traverses the dependency edges and
    includes the phantom node — to verify upgrade behaviour.
    """
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Trigger phantom creation: register a task whose dep doesn't exist yet.
    await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json={
            "task_id": "trigger",
            "task_namespace": "",
            "task_name": "Trigger",
            "task_data": {},
            "dependency_task_ids": ["phantom-target"],
        },
    )

    # Now bulk-register the real task — it should upgrade the phantom row.
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/bulk",
        json={
            "tasks": [
                {
                    "task_id": "phantom-target",
                    "task_namespace": "real",
                    "task_name": "RealTask",
                    "task_data": {"foo": "bar"},
                }
            ]
        },
    )
    assert response.status_code == 201
    upgraded = response.json()["tasks"][0]
    assert upgraded["is_phantom"] is False
    assert upgraded["task_namespace"] == "real"
    assert upgraded["task_name"] == "RealTask"

    # Verify via list endpoint that the upgraded task is now visible
    # (the bulk register associates it with this build via a
    # TASK_REFERENCED event — the row already existed as a phantom, so
    # `task_already_existed=True` and the endpoint emits REFERENCED,
    # not PENDING).
    listed = (await client.get(f"/api/v1/builds/{build_id}/tasks")).json()
    listed_target = [t for t in listed if t["task_id"] == "phantom-target"]
    assert len(listed_target) == 1
    assert listed_target[0]["is_phantom"] is False
    assert listed_target[0]["task_namespace"] == "real"


@pytest.mark.asyncio
async def test_bulk_register_empty_array_returns_201_empty(client: AsyncClient):
    """Empty bulk request is a no-op — returns 201 with an empty
    ``tasks`` array. (201 because the endpoint advertises ``status_code=201``;
    the server doesn't bother distinguishing "0 created" from "N created".)"""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/bulk", json={"tasks": []}
    )
    assert response.status_code == 201
    assert response.json() == {"tasks": []}


@pytest.mark.asyncio
async def test_bulk_register_dedupes_within_batch(client: AsyncClient):
    """Duplicate task_ids within a single bulk request are deduplicated
    (first occurrence wins) so the caller doesn't accidentally generate
    multiple events / repeated dep reconciliation for the same task."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/bulk",
        json={
            "tasks": [
                {
                    "task_id": "dupe-x",
                    "task_namespace": "first",
                    "task_name": "First",
                    "task_data": {"v": 1},
                },
                {
                    "task_id": "dupe-x",
                    "task_namespace": "second",  # Should be ignored.
                    "task_name": "Second",
                    "task_data": {"v": 2},
                },
                {
                    "task_id": "other-y",
                    "task_namespace": "",
                    "task_name": "Other",
                    "task_data": {},
                },
            ]
        },
    )
    assert response.status_code == 201
    returned = response.json()["tasks"]
    # Two unique tasks returned in input order, second occurrence dropped.
    assert [t["task_id"] for t in returned] == ["dupe-x", "other-y"]
    # First occurrence's data wins.
    dupe = next(t for t in returned if t["task_id"] == "dupe-x")
    assert dupe["task_namespace"] == "first"
    assert dupe["task_name"] == "First"
    assert dupe["task_data"] == {"v": 1}

    # Exactly one TASK_PENDING event per unique task_id, not two for dupe-x.
    events = (await client.get(f"/api/v1/builds/{build_id}/events")).json()
    pending_count = sum(1 for e in events if e["event_type"] == "task_pending")
    assert pending_count == 2


@pytest.mark.asyncio
async def test_bulk_register_caps_batch_size(client: AsyncClient):
    """Bulk request over the cap returns 400 rather than processing
    silently — the SDK chunks if it ever has that many tasks."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]
    too_many = [
        {
            "task_id": f"too-many-{i}",
            "task_namespace": "",
            "task_name": "T",
            "task_data": {},
        }
        for i in range(1001)
    ]
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/bulk",
        json={"tasks": too_many},
    )
    assert response.status_code == 400
    assert "limited to" in response.json()["detail"]


@pytest.mark.asyncio
async def test_bulk_register_referenced_event_for_existing_task(
    client: AsyncClient,
):
    """A task that already exists in the env from a prior build gets a
    TASK_REFERENCED event when bulk-registered, not a TASK_PENDING."""
    # Build 1 creates the task.
    r1 = await client.post("/api/v1/builds", json={})
    b1 = r1.json()["id"]
    await client.post(
        f"/api/v1/builds/{b1}/tasks",
        json={
            "task_id": "shared-x",
            "task_namespace": "",
            "task_name": "X",
            "task_data": {},
        },
    )

    # Build 2 bulk-registers the same task.
    r2 = await client.post("/api/v1/builds", json={})
    b2 = r2.json()["id"]
    response = await client.post(
        f"/api/v1/builds/{b2}/tasks/bulk",
        json={
            "tasks": [
                {
                    "task_id": "shared-x",
                    "task_namespace": "",
                    "task_name": "X",
                    "task_data": {},
                }
            ]
        },
    )
    assert response.status_code == 201

    events_response = await client.get(f"/api/v1/builds/{b2}/events")
    event_types = [e["event_type"] for e in events_response.json()]
    assert "task_referenced" in event_types
    assert "task_pending" not in event_types


@pytest.mark.asyncio
async def test_bulk_register_creates_phantom_for_unknown_upstream(
    client: AsyncClient,
):
    """Edges referencing a task that's neither in this batch nor in
    the DB trigger phantom creation (the documented safety hatch).
    Explicit test that the bulk path emits exactly one phantom for the
    unknown upstream, not multiple/none."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Two real tasks both depending on an unknown ``orphan-up`` —
    # deliberately not present in the batch and not pre-registered.
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/bulk",
        json={
            "tasks": [
                {
                    "task_id": "child-a",
                    "task_namespace": "",
                    "task_name": "ChildA",
                    "task_data": {},
                    "dependency_task_ids": ["orphan-up"],
                },
                {
                    "task_id": "child-b",
                    "task_namespace": "",
                    "task_name": "ChildB",
                    "task_data": {},
                    "dependency_task_ids": ["orphan-up"],
                },
            ]
        },
    )
    assert response.status_code == 201

    # Inspect the build's graph: both children should point at one
    # phantom upstream node (not two — the reconcile must dedupe across
    # the batch).
    graph = (
        await client.get(f"/api/v1/builds/{build_id}/graph?upstream_depth=1")
    ).json()
    upstream_nodes = [n for n in graph["nodes"] if n["task_id"] == "orphan-up"]
    assert len(upstream_nodes) == 1, (
        f"Expected exactly one phantom row for orphan-up; saw "
        f"{[n['task_id'] for n in upstream_nodes]}"
    )
    edges_to_orphan = [
        e for e in graph["edges"] if e["source"] == upstream_nodes[0]["id"]
    ]
    assert len(edges_to_orphan) == 2


@pytest.mark.asyncio
async def test_bulk_register_array_order_maps_to_list_order(
    client: AsyncClient,
):
    """The whole point of the post-order array contract: array order in
    POST /tasks/bulk maps directly to the order the UI sees on
    GET /tasks. Single-task path's stable-order test isn't enough — the
    bulk path needs its own assertion."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Deliberately non-alphabetical input order: parent registered last
    # (post-order DFS) — the SDK does this for every batch.
    expected_order = ["leaf-z", "leaf-a", "leaf-m", "parent"]
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/bulk",
        json={
            "tasks": [
                {
                    "task_id": tid,
                    "task_namespace": "",
                    "task_name": tid,
                    "task_data": {},
                    # parent depends on all three leaves; we ordered
                    # them deliberately to make sure the API doesn't
                    # quietly reorder by dep relationship.
                    "dependency_task_ids": (
                        ["leaf-z", "leaf-a", "leaf-m"] if tid == "parent" else []
                    ),
                }
                for tid in expected_order
            ]
        },
    )
    assert response.status_code == 201
    assert [t["task_id"] for t in response.json()["tasks"]] == expected_order

    # And the user-visible list endpoint preserves it.
    listed = (await client.get(f"/api/v1/builds/{build_id}/tasks")).json()
    assert [t["task_id"] for t in listed] == expected_order


@pytest.mark.asyncio
async def test_bulk_register_id_only_returns_slim_response(client: AsyncClient):
    """``?id_only=true`` returns just ``{id, task_id}`` per task,
    skipping task_data / namespace / created_at to cut response size.
    The DB rows are still fully written — only the response payload
    differs."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    payload = {
        "tasks": [
            {
                "task_id": f"slim-task-{i}",
                "task_namespace": "demo",
                "task_name": "SlimTask",
                "task_data": {"big": "x" * 1024, "i": i},
            }
            for i in range(3)
        ]
    }
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/bulk?id_only=true",
        json=payload,
    )
    assert response.status_code == 201
    body = response.json()
    # Slim shape — id + task_id + execution state for re-attach; no
    # task_data / namespace / timestamps.
    assert {tuple(t.keys()) for t in body["tasks"]} == {
        (
            "id",
            "task_id",
            "latest_status",
            "latest_executor",
            "latest_executor_ref",
            "latest_executor_metadata",
        )
    }
    assert [t["task_id"] for t in body["tasks"]] == [
        "slim-task-0",
        "slim-task-1",
        "slim-task-2",
    ]

    # Sanity: the persisted rows are still complete (the slim response
    # is a serialisation choice, not a write-time choice).
    listed = (await client.get(f"/api/v1/builds/{build_id}/tasks")).json()
    persisted = next(t for t in listed if t["task_id"] == "slim-task-0")
    assert persisted["task_namespace"] == "demo"
    assert persisted["task_data"] == {"big": "x" * 1024, "i": 0}


@pytest.mark.asyncio
async def test_bulk_register_default_returns_full_response(client: AsyncClient):
    """Default (``id_only`` omitted or ``false``) returns the full
    ``TaskResponse`` shape — backward compatible with direct API
    callers who rely on the rich response."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/bulk",
        json={
            "tasks": [
                {
                    "task_id": "full-task",
                    "task_namespace": "demo",
                    "task_name": "FullTask",
                    "task_data": {"k": "v"},
                }
            ]
        },
    )
    assert response.status_code == 201
    only_task = response.json()["tasks"][0]
    # Full shape carries task_data, namespace, created_at, is_phantom, …
    assert only_task["task_namespace"] == "demo"
    assert only_task["task_data"] == {"k": "v"}
    assert "created_at" in only_task
    assert only_task["is_phantom"] is False


@pytest.mark.asyncio
async def test_concurrent_bulk_registers_overlapping_tasks(
    client: AsyncClient,
):
    """Two concurrent bulk POSTs into the same environment with
    overlapping task IDs must succeed without deadlocking. We sort the
    SELECT FOR UPDATE by task_id to enforce a deterministic lock-
    acquisition order; this test exercises that path with two clients
    that would otherwise be primed for AABB / BBAA lock interleavings.
    """
    import asyncio

    # Two builds in the same environment.
    b1 = (await client.post("/api/v1/builds", json={})).json()["id"]
    b2 = (await client.post("/api/v1/builds", json={})).json()["id"]

    # Pre-register a couple of cached tasks both batches will
    # re-reference (this triggers the FOR UPDATE lock path on both
    # sides).
    for tid in ("shared-x", "shared-y"):
        await client.post(
            f"/api/v1/builds/{b1}/tasks",
            json={
                "task_id": tid,
                "task_namespace": "",
                "task_name": tid,
                "task_data": {},
            },
        )

    # Build A bulk-registers in shared-x, shared-y order; build B in
    # the reverse order. Without sorted FOR UPDATE acquisition, this
    # is the classic AABB / BBAA deadlock setup.
    payload_a = {
        "tasks": [
            {
                "task_id": "shared-x",
                "task_namespace": "",
                "task_name": "shared-x",
                "task_data": {},
            },
            {
                "task_id": "shared-y",
                "task_namespace": "",
                "task_name": "shared-y",
                "task_data": {},
            },
        ]
    }
    payload_b = {
        "tasks": [
            {
                "task_id": "shared-y",
                "task_namespace": "",
                "task_name": "shared-y",
                "task_data": {},
            },
            {
                "task_id": "shared-x",
                "task_namespace": "",
                "task_name": "shared-x",
                "task_data": {},
            },
        ]
    }
    a_resp, b_resp = await asyncio.gather(
        client.post(f"/api/v1/builds/{b1}/tasks/bulk", json=payload_a),
        client.post(f"/api/v1/builds/{b2}/tasks/bulk", json=payload_b),
    )
    assert a_resp.status_code == 201
    assert b_resp.status_code == 201
    # Both succeeded → no deadlock. (On SQLite, FOR UPDATE is a no-op,
    # so this test is mainly meaningful when CI runs against Postgres,
    # where ``Test Python (stardag-api on Postgres)`` exercises it.)


@pytest.mark.asyncio
async def test_list_tasks_in_build_orders_by_per_build_first_event(
    client: AsyncClient,
):
    """A task that exists from an earlier build should appear at the
    position it was *re-encountered* in the current build, not at the top
    by virtue of having an older Task.created_at.
    """
    # Build 1 creates task A at the global level.
    r1 = await client.post("/api/v1/builds", json={})
    build1_id = r1.json()["id"]
    await client.post(
        f"/api/v1/builds/{build1_id}/tasks",
        json={
            "task_id": "shared-A",
            "task_namespace": "",
            "task_name": "Shared",
            "task_data": {},
        },
    )

    # Build 2 registers a fresh task B *first*, then re-references the
    # existing task A. If we ordered by Task.created_at the order would
    # be [shared-A, fresh-B] (because shared-A was inserted earlier in
    # build 1). With per-build ordering it must be [fresh-B, shared-A].
    r2 = await client.post("/api/v1/builds", json={})
    build2_id = r2.json()["id"]
    await client.post(
        f"/api/v1/builds/{build2_id}/tasks",
        json={
            "task_id": "fresh-B",
            "task_namespace": "",
            "task_name": "Fresh",
            "task_data": {},
        },
    )
    await client.post(
        f"/api/v1/builds/{build2_id}/tasks",
        json={
            "task_id": "shared-A",
            "task_namespace": "",
            "task_name": "Shared",
            "task_data": {},
        },
    )

    response = await client.get(f"/api/v1/builds/{build2_id}/tasks")
    assert response.status_code == 200
    order = [t["task_id"] for t in response.json()]
    assert order == ["fresh-B", "shared-A"], (
        f"Expected per-build first-event order, got {order}"
    )


@pytest.mark.asyncio
async def test_list_tasks_in_build_includes_output_uri(client: AsyncClient):
    """Test that list_tasks_in_build endpoint includes output_uri."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register a task with output_uri
    task_data = {
        "task_id": "output-uri-task",
        "task_namespace": "test",
        "task_name": "OutputUriTask",
        "task_data": {},
        "output_uri": "s3://bucket/output/path.json",
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)

    # Start the task to create an event (so it appears in list_tasks_in_build)
    await client.post(f"/api/v1/builds/{build_id}/tasks/output-uri-task/start")

    # List tasks in build
    response = await client.get(f"/api/v1/builds/{build_id}/tasks")
    assert response.status_code == 200
    tasks = response.json()
    assert len(tasks) == 1
    assert tasks[0]["output_uri"] == "s3://bucket/output/path.json"


@pytest.mark.asyncio
async def test_task_reuse_across_runs(client: AsyncClient):
    """Test that tasks are reused across builds (same task_id in same environment).

    Tasks use global status - when completed in any build, they show as completed
    everywhere. The status_build_id field indicates which build completed the task.
    """
    # Create first run and register task
    response = await client.post("/api/v1/builds", json={})
    build1_id = response.json()["id"]

    task_data = {
        "task_id": "shared-task",
        "task_namespace": "",
        "task_name": "SharedTask",
        "task_data": {"value": 1},
    }
    response = await client.post(f"/api/v1/builds/{build1_id}/tasks", json=task_data)
    task_db_id_1 = response.json()["id"]

    # Create second run and register same task
    response = await client.post("/api/v1/builds", json={})
    build2_id = response.json()["id"]

    response = await client.post(f"/api/v1/builds/{build2_id}/tasks", json=task_data)
    task_db_id_2 = response.json()["id"]

    # Task should be reused (same database ID)
    assert task_db_id_1 == task_db_id_2

    # Complete in build1
    await client.post(f"/api/v1/builds/{build1_id}/tasks/shared-task/start")
    await client.post(f"/api/v1/builds/{build1_id}/tasks/shared-task/complete")

    # Task status in build1 should be completed
    response = await client.get(f"/api/v1/builds/{build1_id}/tasks")
    build1_tasks = response.json()
    build1_task = next(t for t in build1_tasks if t["task_id"] == "shared-task")
    assert build1_task["status"] == "completed"
    assert build1_task["status_build_id"] == build1_id

    # Task status in build2 should also show completed (global status)
    # but status_build_id indicates it was completed in build1
    response = await client.get(f"/api/v1/builds/{build2_id}/tasks")
    build2_tasks = response.json()
    build2_task = next(t for t in build2_tasks if t["task_id"] == "shared-task")
    assert build2_task["status"] == "completed"
    assert (
        build2_task["status_build_id"] == build1_id
    )  # Completed by build1, not build2


# --- commit_hash in event_metadata tests ---


@pytest.mark.asyncio
async def test_task_event_stores_commit_hash(client: AsyncClient):
    """Test that task events store commit_hash in event_metadata."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    task_data = {
        "task_id": "commit-task-1",
        "task_namespace": "",
        "task_name": "CommitTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)

    # Start task with commit_hash
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/commit-task-1/start",
        params={"commit_hash": "abc1234"},
    )
    assert response.status_code == 200

    # Complete task with commit_hash
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/commit-task-1/complete",
        params={"commit_hash": "abc1234"},
    )
    assert response.status_code == 200

    # Check events have commit_hash in metadata
    response = await client.get(f"/api/v1/builds/{build_id}/events")
    events = response.json()
    task_events = [
        e for e in events if e["event_type"] in ("task_started", "task_completed")
    ]
    assert len(task_events) == 2
    for event in task_events:
        assert event["event_metadata"] is not None
        assert event["event_metadata"]["commit_hash"] == "abc1234"


@pytest.mark.asyncio
async def test_task_event_without_commit_hash(client: AsyncClient):
    """Test that task events without commit_hash have no event_metadata."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    task_data = {
        "task_id": "no-commit-task",
        "task_namespace": "",
        "task_name": "NoCommitTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)

    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/no-commit-task/start"
    )
    assert response.status_code == 200

    # Events should have no event_metadata
    response = await client.get(f"/api/v1/builds/{build_id}/events")
    events = response.json()
    start_event = next(e for e in events if e["event_type"] == "task_started")
    assert start_event["event_metadata"] is None


@pytest.mark.asyncio
async def test_commit_hash_in_task_list_response(client: AsyncClient):
    """Test that commit_hash from completion event appears in task list response."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    task_data = {
        "task_id": "commit-list-task",
        "task_namespace": "",
        "task_name": "CommitListTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/commit-list-task/start",
        params={"commit_hash": "start123"},
    )
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/commit-list-task/complete",
        params={"commit_hash": "done456"},
    )

    # Task list should show commit_hash from completion event
    response = await client.get(f"/api/v1/builds/{build_id}/tasks")
    tasks = response.json()
    task = next(t for t in tasks if t["task_id"] == "commit-list-task")
    assert task["commit_hash"] == "done456"


@pytest.mark.asyncio
async def test_build_event_stores_commit_hash(client: AsyncClient):
    """Test that build events store commit_hash in event_metadata."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Complete build with commit_hash
    response = await client.post(
        f"/api/v1/builds/{build_id}/complete",
        params={"commit_hash": "build-abc"},
    )
    assert response.status_code == 200

    # Check events
    response = await client.get(f"/api/v1/builds/{build_id}/events")
    events = response.json()
    complete_event = next(e for e in events if e["event_type"] == "build_completed")
    assert complete_event["event_metadata"] is not None
    assert complete_event["event_metadata"]["commit_hash"] == "build-abc"


@pytest.mark.asyncio
async def test_commit_hash_with_resumed_build(client: AsyncClient):
    """Test commit_hash tracking across builds (simulating resume scenario).

    When a task fails in build1 at commit A and completes in build2 at commit B,
    the task's commit_hash should reflect commit B.
    """
    # Build 1: task fails at commit A
    response = await client.post("/api/v1/builds", json={"commit_hash": "commit-A"})
    build1_id = response.json()["id"]

    task_data = {
        "task_id": "resume-task",
        "task_namespace": "",
        "task_name": "ResumeTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build1_id}/tasks", json=task_data)
    await client.post(
        f"/api/v1/builds/{build1_id}/tasks/resume-task/start",
        params={"commit_hash": "commit-A"},
    )
    await client.post(
        f"/api/v1/builds/{build1_id}/tasks/resume-task/fail",
        params={"error_message": "oops", "commit_hash": "commit-A"},
    )

    # Build 2 (resume): task completes at commit B
    response = await client.post("/api/v1/builds", json={"commit_hash": "commit-B"})
    build2_id = response.json()["id"]

    # Re-register task in build2
    await client.post(f"/api/v1/builds/{build2_id}/tasks", json=task_data)
    await client.post(
        f"/api/v1/builds/{build2_id}/tasks/resume-task/start",
        params={"commit_hash": "commit-B"},
    )
    await client.post(
        f"/api/v1/builds/{build2_id}/tasks/resume-task/complete",
        params={"commit_hash": "commit-B"},
    )

    # Global task status should show commit B (from completion event)
    response = await client.get(f"/api/v1/builds/{build2_id}/tasks")
    tasks = response.json()
    task = next(t for t in tasks if t["task_id"] == "resume-task")
    assert task["status"] == "completed"
    assert task["commit_hash"] == "commit-B"
    assert task["status_build_id"] == build2_id


# --- Dynamic dependency registration (POST /tasks/{task_id}/dependencies) ---


@pytest.mark.asyncio
async def test_add_task_dependencies_creates_edges(client: AsyncClient):
    """Dynamically-yielded deps register edges and show up in the graph."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register the downstream (orchestrator) task with no static deps
    orch = {
        "task_id": "orch",
        "task_name": "Orchestrator",
        "task_data": {},
        "dependency_task_ids": [],
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=orch)

    # Dynamically record a dep via the new endpoint (upstream has not been
    # registered yet — endpoint should create a phantom)
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/orch/dependencies",
        json={"upstream_task_ids": ["yielded-dep"], "is_dynamic": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"added": 1, "total": 1}

    # Register the yielded dep properly so it appears in the build graph
    await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json={
            "task_id": "yielded-dep",
            "task_name": "Yielded",
            "task_data": {},
            "dependency_task_ids": [],
        },
    )

    # Graph endpoint should return the edge with is_dynamic=True
    response = await client.get(f"/api/v1/builds/{build_id}/graph")
    assert response.status_code == 200
    graph = response.json()
    edges = graph["edges"]
    assert len(edges) == 1
    edge = edges[0]
    assert edge["is_dynamic"] is True


@pytest.mark.asyncio
async def test_add_task_dependencies_idempotent(client: AsyncClient):
    """Repeat calls with the same edge are idempotent (added=0 second time)."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json={"task_id": "d", "task_name": "D", "task_data": {}},
    )

    payload = {"upstream_task_ids": ["u1", "u2"], "is_dynamic": True}
    first = await client.post(
        f"/api/v1/builds/{build_id}/tasks/d/dependencies", json=payload
    )
    assert first.json() == {"added": 2, "total": 2}

    second = await client.post(
        f"/api/v1/builds/{build_id}/tasks/d/dependencies", json=payload
    )
    # Same edges — nothing new inserted
    assert second.json() == {"added": 0, "total": 2}


@pytest.mark.asyncio
async def test_static_deps_graph_has_is_dynamic_false(client: AsyncClient):
    """Edges created via the standard task_register path are marked is_dynamic=False."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register a downstream with a static dep
    await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json={
            "task_id": "down",
            "task_name": "Down",
            "task_data": {},
            "dependency_task_ids": ["up"],
        },
    )
    # Register the upstream so it's in the build
    await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json={"task_id": "up", "task_name": "Up", "task_data": {}},
    )

    response = await client.get(f"/api/v1/builds/{build_id}/graph")
    edges = response.json()["edges"]
    assert len(edges) == 1
    assert edges[0]["is_dynamic"] is False


@pytest.mark.asyncio
async def test_add_task_dependencies_unknown_task_returns_404(client: AsyncClient):
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/nonexistent/dependencies",
        json={"upstream_task_ids": ["u1"], "is_dynamic": True},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_start_task_with_executor_ref(client: AsyncClient):
    """Executor refs on /start are recorded and surfaced for re-attach.

    The ref lands in the TASK_STARTED event metadata and the denormalised
    task columns, and a subsequent bulk registration (e.g. a resumed
    build's discovery pass) gets it back in the slim response so the build
    engine can re-attach to the detached execution.
    """
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    task_data = {
        "task_id": "detached-task",
        "task_namespace": "",
        "task_name": "DetachedTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/detached-task/start",
        params={"executor": "modal", "executor_ref": "fc-123abc"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "running"

    # A resumed build's bulk registration sees the running execution's ref.
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/bulk?id_only=true",
        json={"tasks": [task_data]},
    )
    assert response.status_code == 201
    ref = response.json()["tasks"][0]
    assert ref["task_id"] == "detached-task"
    assert ref["latest_status"] == "running"
    assert ref["latest_executor"] == "modal"
    assert ref["latest_executor_ref"] == "fc-123abc"

    # The event metadata carries the ref too (event-sourced ground truth).
    events = (await client.get(f"/api/v1/builds/{build_id}/events")).json()
    started = [e for e in events if e["event_type"] == "task_started"]
    assert len(started) == 1


@pytest.mark.asyncio
async def test_start_task_without_ref_clears_stale_executor_ref(client: AsyncClient):
    """A TASK_STARTED without executor info clears a previously recorded ref.

    Guards against a resumed build re-attaching to a stale function call
    from an earlier detached run after the task was restarted non-detached.
    """
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    task_data = {
        "task_id": "restarted-task",
        "task_namespace": "",
        "task_name": "RestartedTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/restarted-task/start",
        params={"executor": "modal", "executor_ref": "fc-old"},
    )
    # Restarted without a detached ref (e.g. local executor this time).
    await client.post(f"/api/v1/builds/{build_id}/tasks/restarted-task/start")

    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/bulk?id_only=true",
        json={"tasks": [task_data]},
    )
    ref = response.json()["tasks"][0]
    assert ref["latest_status"] == "running"
    assert ref["latest_executor"] is None
    assert ref["latest_executor_ref"] is None


@pytest.mark.asyncio
async def test_notify_build_set_and_clear(client: AsyncClient):
    """Workers set the scheduler wake-up flag; a tick clears it."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]

    response = await client.post(f"/api/v1/builds/{build_id}/notify")
    assert response.status_code == 200
    assert response.json()["needs_tick"] is True

    response = await client.get(f"/api/v1/builds/{build_id}/frontier")
    assert response.json()["needs_tick"] is True

    response = await client.delete(f"/api/v1/builds/{build_id}/notify")
    assert response.status_code == 200
    assert response.json()["needs_tick"] is False

    response = await client.get(f"/api/v1/builds/{build_id}/frontier")
    assert response.json()["needs_tick"] is False


@pytest.mark.asyncio
async def test_frontier_dependency_gating_and_counts(client: AsyncClient):
    """The frontier only exposes tasks whose upstreams are all completed,
    and reports per-status counts + root statuses for terminal detection."""
    build_data = {"root_task_ids": ["frontier-root"]}
    build_id = (await client.post("/api/v1/builds", json=build_data)).json()["id"]

    # dep -> root chain
    await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json={
            "task_id": "frontier-dep",
            "task_namespace": "",
            "task_name": "Dep",
            "task_data": {},
        },
    )
    await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json={
            "task_id": "frontier-root",
            "task_namespace": "",
            "task_name": "Root",
            "task_data": {},
            "dependency_task_ids": ["frontier-dep"],
        },
    )

    # Initially only the dep is actionable (root blocked by incomplete dep).
    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert [t["task_id"] for t in frontier["actionable"]] == ["frontier-dep"]
    assert frontier["status_counts"] == {"pending": 2}
    assert frontier["root_task_ids"] == ["frontier-root"]
    assert frontier["roots"][0]["latest_status"] == "pending"
    assert frontier["build_status"] == "running"

    # Dep starts (with a detached ref) — still actionable (RUNNING is
    # returned so the scheduler can verify ref liveness), root still blocked.
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/frontier-dep/start",
        params={"executor": "modal", "executor_ref": "fc-dep-1"},
    )
    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    actionable = {t["task_id"]: t for t in frontier["actionable"]}
    assert set(actionable) == {"frontier-dep"}
    assert actionable["frontier-dep"]["latest_status"] == "running"
    assert actionable["frontier-dep"]["latest_executor_ref"] == "fc-dep-1"

    # Dep completes — root becomes actionable.
    await client.post(f"/api/v1/builds/{build_id}/tasks/frontier-dep/complete")
    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert [t["task_id"] for t in frontier["actionable"]] == ["frontier-root"]
    assert frontier["status_counts"] == {"completed": 1, "pending": 1}

    # Root completes — nothing actionable, roots all completed.
    await client.post(f"/api/v1/builds/{build_id}/tasks/frontier-root/start")
    await client.post(f"/api/v1/builds/{build_id}/tasks/frontier-root/complete")
    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert frontier["actionable"] == []
    assert frontier["status_counts"] == {"completed": 2}
    assert frontier["roots"][0]["latest_status"] == "completed"


@pytest.mark.asyncio
async def test_frontier_includes_suspended_with_complete_dynamic_deps(
    client: AsyncClient,
):
    """A suspended task becomes actionable once its dynamically-added deps
    complete (dynamic edges gate exactly like static ones)."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]

    await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json={
            "task_id": "dyn-parent",
            "task_namespace": "",
            "task_name": "Parent",
            "task_data": {},
        },
    )
    await client.post(f"/api/v1/builds/{build_id}/tasks/dyn-parent/start")
    # Parent yields a dynamic dep and suspends.
    await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json={
            "task_id": "dyn-child",
            "task_namespace": "",
            "task_name": "Child",
            "task_data": {},
        },
    )
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/dyn-parent/dependencies",
        json={"upstream_task_ids": ["dyn-child"], "is_dynamic": True},
    )
    await client.post(f"/api/v1/builds/{build_id}/tasks/dyn-parent/suspend")

    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert [t["task_id"] for t in frontier["actionable"]] == ["dyn-child"]

    # Child completes → suspended parent becomes actionable for re-invocation.
    await client.post(f"/api/v1/builds/{build_id}/tasks/dyn-child/start")
    await client.post(f"/api/v1/builds/{build_id}/tasks/dyn-child/complete")
    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert [t["task_id"] for t in frontier["actionable"]] == ["dyn-parent"]
    assert frontier["actionable"][0]["latest_status"] == "suspended"


@pytest.mark.asyncio
async def test_frontier_build_not_found_and_notify_404(client: AsyncClient):
    fake = "00000000-0000-0000-0000-000000000099"
    assert (await client.get(f"/api/v1/builds/{fake}/frontier")).status_code == 404
    assert (await client.post(f"/api/v1/builds/{fake}/notify")).status_code == 404


@pytest.mark.asyncio
async def test_non_reactive_build_has_null_reactive_fields(client: AsyncClient):
    """A plain build is not reactively scheduled: reactive_app_name (the
    marker) is null on both the build response and the frontier."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]

    build = (await client.get(f"/api/v1/builds/{build_id}")).json()
    assert build["reactive_app_name"] is None
    assert build["reactive_tick_kwargs"] is None

    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert frontier["reactive_app_name"] is None
    assert frontier["reactive_tick_kwargs"] is None


@pytest.mark.asyncio
async def test_set_reactive_meta_appears_on_build_and_frontier(client: AsyncClient):
    """PUT /reactive-meta marks the build reactively scheduled (sets
    reactive_app_name); the config is exposed on the build response and the
    frontier (where a tick reads it). The endpoint is an idempotent upsert —
    a re-trigger may update tick_kwargs."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]

    response = await client.put(
        f"/api/v1/builds/{build_id}/reactive-meta",
        json={"app_name": "my-app", "tick_kwargs": {"linger_seconds": 30}},
    )
    assert response.status_code == 200
    assert response.json()["reactive_app_name"] == "my-app"
    assert response.json()["reactive_tick_kwargs"] == {"linger_seconds": 30}

    build = (await client.get(f"/api/v1/builds/{build_id}")).json()
    assert build["reactive_app_name"] == "my-app"
    assert build["reactive_tick_kwargs"] == {"linger_seconds": 30}

    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert frontier["reactive_app_name"] == "my-app"
    assert frontier["reactive_tick_kwargs"] == {"linger_seconds": 30}

    # Upsert: a re-trigger updates tick_kwargs (and may change the owner).
    response = await client.put(
        f"/api/v1/builds/{build_id}/reactive-meta",
        json={"app_name": "my-app-2", "tick_kwargs": {"fail_mode": "continue"}},
    )
    assert response.status_code == 200
    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert frontier["reactive_app_name"] == "my-app-2"
    assert frontier["reactive_tick_kwargs"] == {"fail_mode": "continue"}


@pytest.mark.asyncio
async def test_bare_retrigger_preserves_tick_kwargs(client: AsyncClient):
    """A re-trigger with tick_kwargs omitted preserves the stored config
    (regression: a bare PUT used to wipe it to {}); a re-trigger that passes
    tick_kwargs updates it."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]

    # Initial trigger with tick_kwargs.
    await client.put(
        f"/api/v1/builds/{build_id}/reactive-meta",
        json={"app_name": "my-app", "tick_kwargs": {"linger_seconds": 30}},
    )

    # Bare re-trigger (no tick_kwargs) — the stored config must survive.
    response = await client.put(
        f"/api/v1/builds/{build_id}/reactive-meta",
        json={"app_name": "my-app"},
    )
    assert response.status_code == 200
    assert response.json()["reactive_app_name"] == "my-app"
    assert response.json()["reactive_tick_kwargs"] == {"linger_seconds": 30}

    # Explicit tick_kwargs on re-trigger — updates the config.
    response = await client.put(
        f"/api/v1/builds/{build_id}/reactive-meta",
        json={"app_name": "my-app", "tick_kwargs": {"linger_seconds": 5}},
    )
    assert response.status_code == 200
    assert response.json()["reactive_tick_kwargs"] == {"linger_seconds": 5}


@pytest.mark.asyncio
async def test_list_builds_reactive_app_name_and_status_filter(client: AsyncClient):
    """GET /builds?reactive_app_name=X&status=running returns only the named
    app's reactively-scheduled builds that are currently RUNNING — the
    watchdog's real query. (Builds are RUNNING on creation; /complete flips
    a build to a non-running status.)"""
    # A running reactive build owned by app-x.
    running_x = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.put(
        f"/api/v1/builds/{running_x}/reactive-meta",
        json={"app_name": "app-x", "tick_kwargs": {}},
    )

    # A reactive build owned by app-x but completed (not running).
    done_x = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.put(
        f"/api/v1/builds/{done_x}/reactive-meta",
        json={"app_name": "app-x", "tick_kwargs": {}},
    )
    await client.post(f"/api/v1/builds/{done_x}/complete")

    # A running reactive build owned by a DIFFERENT app.
    running_y = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.put(
        f"/api/v1/builds/{running_y}/reactive-meta",
        json={"app_name": "app-y", "tick_kwargs": {}},
    )

    # A running NON-reactive build.
    running_plain = (await client.post("/api/v1/builds", json={})).json()["id"]

    # reactive_app_name alone: both app-x reactive builds (any status).
    resp = (
        await client.get("/api/v1/builds", params={"reactive_app_name": "app-x"})
    ).json()
    assert {b["id"] for b in resp["builds"]} == {running_x, done_x}

    # reactive_app_name + status=running: only the running app-x build.
    resp = (
        await client.get(
            "/api/v1/builds",
            params={"reactive_app_name": "app-x", "status": "running"},
        )
    ).json()
    assert [b["id"] for b in resp["builds"]] == [running_x]
    assert resp["total"] == 1

    # status alone still filters (across all builds in the env).
    resp = (await client.get("/api/v1/builds", params={"status": "running"})).json()
    running_ids = {b["id"] for b in resp["builds"]}
    assert running_x in running_ids
    assert running_y in running_ids
    assert running_plain in running_ids
    assert done_x not in running_ids


@pytest.mark.asyncio
async def test_set_reactive_meta_build_not_found(client: AsyncClient):
    fake = "00000000-0000-0000-0000-000000000099"
    response = await client.put(
        f"/api/v1/builds/{fake}/reactive-meta",
        json={"app_name": "my-app", "tick_kwargs": {}},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_set_reactive_meta_environment_isolation(
    client: AsyncClient, as_environment_b
):
    """reactive-meta is environment-scoped: another environment's auth
    cannot set it on this build (403)."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]

    with as_environment_b():
        response = await client.put(
            f"/api/v1/builds/{build_id}/reactive-meta",
            json={"app_name": "my-app", "tick_kwargs": {}},
        )
        assert response.status_code == 403

    # Untouched in the owning environment.
    build = (await client.get(f"/api/v1/builds/{build_id}")).json()
    assert build["reactive_app_name"] is None


def _register_payload(task_id: str, deps: list[str] | None = None) -> dict:
    return {
        "task_id": task_id,
        "task_namespace": "",
        "task_name": "T",
        "task_data": {},
        "dependency_task_ids": deps or [],
    }


@pytest.mark.asyncio
async def test_retry_task_resets_failed_to_pending(client: AsyncClient):
    """TASK_RETRIED flips failed/cancelled/skipped back to pending (global
    and per-build views) so the task is schedulable again."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("retry-t")
    )
    await client.post(f"/api/v1/builds/{build_id}/tasks/retry-t/start")
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/retry-t/fail",
        params={"error_message": "boom"},
    )

    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert frontier["status_counts"] == {"failed": 1}

    response = await client.post(f"/api/v1/builds/{build_id}/tasks/retry-t/retry")
    assert response.status_code == 200
    assert response.json()["status"] == "pending"

    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert frontier["status_counts"] == {"pending": 1}
    assert [t["task_id"] for t in frontier["actionable"]] == ["retry-t"]
    # Executor ref of the failed run was cleared with the retry.
    assert frontier["actionable"][0]["latest_executor_ref"] is None


@pytest.mark.asyncio
async def test_retry_task_never_downgrades_completed_or_running(client: AsyncClient):
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    for tid in ("retry-done", "retry-running"):
        await client.post(
            f"/api/v1/builds/{build_id}/tasks", json=_register_payload(tid)
        )
    await client.post(f"/api/v1/builds/{build_id}/tasks/retry-done/start")
    await client.post(f"/api/v1/builds/{build_id}/tasks/retry-done/complete")
    await client.post(f"/api/v1/builds/{build_id}/tasks/retry-running/start")

    r1 = await client.post(f"/api/v1/builds/{build_id}/tasks/retry-done/retry")
    r2 = await client.post(f"/api/v1/builds/{build_id}/tasks/retry-running/retry")
    assert r1.json()["status"] == "completed"
    # A running task holds a live execution claim: releasing it is
    # cancellation, not retry — a reset here would invite a second execution.
    assert r2.json()["status"] == "running"


@pytest.mark.asyncio
async def test_retry_task_resets_suspended_to_pending(client: AsyncClient):
    """A task suspended for dynamic dependencies and then abandoned is
    recoverable by retry — otherwise it is permanently unschedulable."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("susp-t")
    )
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/susp-t/start",
        params={"executor": "modal", "executor_ref": "fc-suspended"},
    )
    await client.post(f"/api/v1/builds/{build_id}/tasks/susp-t/suspend")

    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert frontier["status_counts"] == {"suspended": 1}

    response = await client.post(f"/api/v1/builds/{build_id}/tasks/susp-t/retry")
    assert response.status_code == 200
    assert response.json()["status"] == "pending"

    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert frontier["status_counts"] == {"pending": 1}
    assert [t["task_id"] for t in frontier["actionable"]] == ["susp-t"]
    # The suspended run's executor ref is cleared with the retry, so no
    # scheduler re-attaches to an execution that already returned.
    assert frontier["actionable"][0]["latest_executor"] is None
    assert frontier["actionable"][0]["latest_executor_ref"] is None

    # Per-build derived status (the event replay) agrees with the
    # denormalised global one.
    tasks = (await client.get(f"/api/v1/builds/{build_id}/tasks")).json()
    assert [t["status"] for t in tasks] == ["pending"]


@pytest.mark.asyncio
async def test_add_build_roots_appends_dedup(client: AsyncClient):
    build_id = (
        await client.post("/api/v1/builds", json={"root_task_ids": ["r1"]})
    ).json()["id"]

    response = await client.post(
        f"/api/v1/builds/{build_id}/roots",
        json={"root_task_ids": ["r2", "r1", "r3"]},
    )
    assert response.status_code == 200
    assert response.json()["root_task_ids"] == ["r1", "r2", "r3"]

    # Frontier terminal-detection input covers the added roots.
    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert frontier["root_task_ids"] == ["r1", "r2", "r3"]


@pytest.mark.asyncio
async def test_frontier_running_includes_non_actionable(client: AsyncClient):
    """A RUNNING task with an incomplete upstream (dynamic-dep window) is
    excluded from `actionable` but still listed in `running` — cancellation
    must reach it."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("dyn-blocker")
    )
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("dyn-runner")
    )
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/dyn-runner/start",
        params={"executor": "modal", "executor_ref": "fc-dyn"},
    )
    # dynamic edge lands while the task is still RUNNING
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/dyn-runner/dependencies",
        json={"upstream_task_ids": ["dyn-blocker"], "is_dynamic": True},
    )

    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    actionable_ids = [t["task_id"] for t in frontier["actionable"]]
    running_ids = [t["task_id"] for t in frontier["running"]]
    assert "dyn-runner" not in actionable_ids  # blocked by incomplete upstream
    assert running_ids == ["dyn-runner"]
    assert frontier["running"][0]["latest_executor_ref"] == "fc-dyn"


@pytest.mark.asyncio
async def test_frontier_reflects_cross_build_running(client: AsyncClient):
    """A task started by ANOTHER build shows RUNNING (with its ref) in this
    build's frontier — the cross-build semantics the endpoint advertises."""
    build_a = (await client.post("/api/v1/builds", json={})).json()["id"]
    build_b = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_a}/tasks", json=_register_payload("shared-task")
    )
    await client.post(
        f"/api/v1/builds/{build_b}/tasks", json=_register_payload("shared-task")
    )
    await client.post(
        f"/api/v1/builds/{build_a}/tasks/shared-task/start",
        params={"executor": "modal", "executor_ref": "fc-other-build"},
    )

    frontier_b = (await client.get(f"/api/v1/builds/{build_b}/frontier")).json()
    actionable = {t["task_id"]: t for t in frontier_b["actionable"]}
    assert actionable["shared-task"]["latest_status"] == "running"
    assert actionable["shared-task"]["latest_executor_ref"] == "fc-other-build"


async def _assert_frontier_reports_cross_build_blocker(client: AsyncClient) -> None:
    """A build whose only remaining task is gated by an upstream that another
    build left RUNNING has nothing actionable and nothing running — the
    frontier must say so explicitly instead of looking terminal."""
    build_a = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_a}/tasks",
        json={
            "task_id": "ext-up",
            "task_namespace": "pipelines.ingest",
            "task_name": "Upstream",
            "task_data": {},
        },
    )
    await client.post(
        f"/api/v1/builds/{build_a}/tasks",
        json=_register_payload("ext-down", ["ext-up"]),
    )
    await client.post(
        f"/api/v1/builds/{build_a}/tasks/ext-up/start",
        params={"executor": "modal", "executor_ref": "fc-build-a"},
    )

    # Build B references only the downstream task; the upstream (and the
    # edge, which is environment-global) belongs to build A entirely.
    build_b = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_b}/tasks", json=_register_payload("ext-down")
    )

    frontier_b = (await client.get(f"/api/v1/builds/{build_b}/frontier")).json()
    # The shape that used to read as "this build cannot progress".
    assert frontier_b["actionable"] == []
    assert frontier_b["running"] == []
    assert frontier_b["status_counts"] == {"pending": 1}

    assert frontier_b["blocked_by_external_truncated"] is False
    assert frontier_b["blocked_by_external"] == [
        {
            "task_id": "ext-down",
            "blocking_task_id": "ext-up",
            "blocking_task_namespace": "pipelines.ingest",
            "blocking_task_name": "Upstream",
            "blocking_status": "running",
            "blocking_status_at": frontier_b["blocked_by_external"][0][
                "blocking_status_at"
            ],
            "blocking_status_expires_at": frontier_b["blocked_by_external"][0][
                "blocking_status_expires_at"
            ],
            "blocking_status_build_id": build_a,
            "blocking_in_build": False,
        }
    ]
    assert frontier_b["blocked_by_external"][0]["blocking_status_at"] is not None
    # The blocker holds a live claim, so build B is told when it lapses —
    # the one liveness question B cannot answer for itself (it cannot probe
    # build A's executor).
    assert (
        frontier_b["blocked_by_external"][0]["blocking_status_expires_at"] is not None
    )

    # Build A owns the blocker, so from its side nothing is external — and
    # its own liveness signal (`running`) covers the same task.
    frontier_a = (await client.get(f"/api/v1/builds/{build_a}/frontier")).json()
    assert frontier_a["blocked_by_external"] == []
    assert [t["task_id"] for t in frontier_a["running"]] == ["ext-up"]

    # Once the blocker completes, the downstream is this build's to run.
    await client.post(f"/api/v1/builds/{build_a}/tasks/ext-up/complete")
    frontier_b = (await client.get(f"/api/v1/builds/{build_b}/frontier")).json()
    assert frontier_b["blocked_by_external"] == []
    assert [t["task_id"] for t in frontier_b["actionable"]] == ["ext-down"]


@pytest.mark.asyncio
async def test_frontier_reports_blocker_owned_by_another_build(client: AsyncClient):
    await _assert_frontier_reports_cross_build_blocker(client)


@pytest.mark.asyncio
async def test_frontier_reports_blocker_owned_by_another_build_postgres(pg_client):
    """Same scenario on Postgres: the "not this build's status" predicate is
    a null-safe inequality, which the two dialects render differently
    (IS DISTINCT FROM vs IS NOT)."""
    await _assert_frontier_reports_cross_build_blocker(pg_client)


@pytest.mark.asyncio
async def test_frontier_external_blocker_reports_in_build_membership(
    client: AsyncClient,
):
    """`blocking_in_build` separates the two ways a stalled build is held up.

    Chain top -> mid -> down, where `top` runs under another build and `mid`
    is registered here but was last touched there (registration is
    status-neutral, so ownership does not move). This build is stalled on
    both: one blocker it has never heard of, one it can see but cannot
    advance.
    """
    build_a = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_a}/tasks", json=_register_payload("chain-top")
    )
    await client.post(
        f"/api/v1/builds/{build_a}/tasks",
        json=_register_payload("chain-mid", ["chain-top"]),
    )
    await client.post(f"/api/v1/builds/{build_a}/tasks/chain-top/start")

    build_b = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_b}/tasks", json=_register_payload("chain-mid")
    )
    await client.post(
        f"/api/v1/builds/{build_b}/tasks",
        json=_register_payload("chain-down", ["chain-mid"]),
    )

    frontier_b = (await client.get(f"/api/v1/builds/{build_b}/frontier")).json()
    assert frontier_b["actionable"] == []
    assert frontier_b["running"] == []

    entries = {e["task_id"]: e for e in frontier_b["blocked_by_external"]}
    # Never registered here: this build can only wait for whoever owns it.
    assert entries["chain-mid"]["blocking_task_id"] == "chain-top"
    assert entries["chain-mid"]["blocking_in_build"] is False
    assert entries["chain-mid"]["blocking_status"] == "running"
    assert entries["chain-mid"]["blocking_status_build_id"] == build_a
    # Registered here, but held back by the above — visible, not advanceable.
    assert entries["chain-down"]["blocking_task_id"] == "chain-mid"
    assert entries["chain-down"]["blocking_in_build"] is True


@pytest.mark.asyncio
async def test_frontier_omits_external_blockers_while_the_build_progresses(
    client: AsyncClient,
):
    """The blocker list is computed only when the build has nothing
    actionable and nothing running.

    That is the single state in which "why is this not progressing?" is a
    real question, and the gate keeps a per-edge sort off the hot path: the
    frontier is re-read on every linger poll of every healthy build.
    """
    build_a = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_a}/tasks", json=_register_payload("gate-up")
    )
    await client.post(
        f"/api/v1/builds/{build_a}/tasks",
        json=_register_payload("gate-down", ["gate-up"]),
    )
    await client.post(f"/api/v1/builds/{build_a}/tasks/gate-up/start")

    build_b = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_b}/tasks", json=_register_payload("gate-down")
    )
    await client.post(
        f"/api/v1/builds/{build_b}/tasks", json=_register_payload("gate-free")
    )

    # Something to run: not stalled, so no diagnostic even though the
    # external blocker is present and gating `gate-down`.
    frontier_b = (await client.get(f"/api/v1/builds/{build_b}/frontier")).json()
    assert [t["task_id"] for t in frontier_b["actionable"]] == ["gate-free"]
    assert frontier_b["blocked_by_external"] == []

    # Running counts as progress too. (A RUNNING task stays in `actionable`
    # — that is the scheduler's probe partition — so this also pins down
    # that the gate is an OR, not a check on `actionable` alone.)
    await client.post(f"/api/v1/builds/{build_b}/tasks/gate-free/start")
    frontier_b = (await client.get(f"/api/v1/builds/{build_b}/frontier")).json()
    assert [t["task_id"] for t in frontier_b["running"]] == ["gate-free"]
    assert frontier_b["blocked_by_external"] == []

    # Out of work: now the build looks stuck, and the answer appears.
    await client.post(f"/api/v1/builds/{build_b}/tasks/gate-free/complete")
    frontier_b = (await client.get(f"/api/v1/builds/{build_b}/frontier")).json()
    assert frontier_b["actionable"] == []
    assert frontier_b["running"] == []
    assert [e["task_id"] for e in frontier_b["blocked_by_external"]] == ["gate-down"]


@pytest.mark.asyncio
async def test_frontier_external_blockers_are_capped_and_flagged(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """The blocker list is a bounded diagnostic; truncation is reported
    rather than silent."""
    from stardag_api.routes import builds as builds_routes

    monkeypatch.setattr(builds_routes, "_MAX_FRONTIER_EXTERNAL_BLOCKERS", 1)

    build_a = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_a}/tasks", json=_register_payload("cap-up")
    )
    for tid in ("cap-down-1", "cap-down-2"):
        await client.post(
            f"/api/v1/builds/{build_a}/tasks", json=_register_payload(tid, ["cap-up"])
        )
    await client.post(f"/api/v1/builds/{build_a}/tasks/cap-up/start")

    build_b = (await client.post("/api/v1/builds", json={})).json()["id"]
    for tid in ("cap-down-1", "cap-down-2"):
        await client.post(
            f"/api/v1/builds/{build_b}/tasks", json=_register_payload(tid)
        )

    frontier_b = (await client.get(f"/api/v1/builds/{build_b}/frontier")).json()
    assert len(frontier_b["blocked_by_external"]) == 1
    assert frontier_b["blocked_by_external_truncated"] is True
    # Registration order, so the cap keeps a stable prefix across polls.
    assert frontier_b["blocked_by_external"][0]["task_id"] == "cap-down-1"


@pytest.mark.asyncio
async def test_frontier_refs_carry_status_timestamp(client: AsyncClient):
    """`latest_status_at` is the input to scheduler staleness bounds (e.g.
    'RUNNING too long with no executor ref'); it must actually be sent."""
    build_id = (
        await client.post("/api/v1/builds", json={"root_task_ids": ["ts-task"]})
    ).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("ts-task")
    )
    await client.post(f"/api/v1/builds/{build_id}/tasks/ts-task/start")

    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    for key in ("actionable", "running", "roots"):
        assert frontier[key], key
        assert frontier[key][0]["latest_status_at"] is not None, key


@pytest.mark.asyncio
async def test_concurrency_limit_crud(client: AsyncClient):
    response = await client.get("/api/v1/concurrency-limits")
    assert response.status_code == 200 and response.json()["limits"] == []

    response = await client.put(
        "/api/v1/concurrency-limits/gpu", json={"max_concurrent": 2}
    )
    assert response.status_code == 200
    assert response.json() == {"key": "gpu", "max_concurrent": 2}

    # upsert updates in place
    await client.put("/api/v1/concurrency-limits/gpu", json={"max_concurrent": 3})
    limits = (await client.get("/api/v1/concurrency-limits")).json()["limits"]
    assert limits == [{"key": "gpu", "max_concurrent": 3}]

    assert (
        await client.put("/api/v1/concurrency-limits/bad", json={"max_concurrent": 0})
    ).status_code == 422

    assert (await client.delete("/api/v1/concurrency-limits/gpu")).status_code == 204
    assert (await client.delete("/api/v1/concurrency-limits/gpu")).status_code == 404


@pytest.mark.asyncio
async def test_concurrency_limit_enforced_on_start(client: AsyncClient):
    """At-capacity keys reject enforced starts with 409; slots free when a
    holder reaches a terminal status; unconfigured keys are unlimited."""
    await client.put("/api/v1/concurrency-limits/db", json={"max_concurrent": 1})
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    for tid in ("lim-a", "lim-b", "lim-c"):
        await client.post(
            f"/api/v1/builds/{build_id}/tasks", json=_register_payload(tid)
        )

    # First acquire fills the single slot.
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/lim-a/start",
        params={"limit_key": ["db"], "enforce_limits": "true"},
    )
    assert response.status_code == 200

    # Second is denied — no event recorded, task stays pending.
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/lim-b/start",
        params={"limit_key": ["db"], "enforce_limits": "true"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "concurrency_limit_reached"
    assert response.json()["detail"]["denied_keys"] == ["db"]
    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    statuses = {t["task_id"]: t["latest_status"] for t in frontier["actionable"]}
    assert statuses["lim-b"] == "pending"

    # Unconfigured key: unlimited.
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/lim-c/start",
        params={"limit_key": ["other"], "enforce_limits": "true"},
    )
    assert response.status_code == 200

    # Re-starting the holder (e.g. recording an executor ref) never
    # self-blocks.
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/lim-a/start",
        params={
            "limit_key": ["db"],
            "enforce_limits": "true",
            "executor": "modal",
            "executor_ref": "fc-1",
        },
    )
    assert response.status_code == 200

    # Holder completes → slot freed → the denied task can start.
    await client.post(f"/api/v1/builds/{build_id}/tasks/lim-a/complete")
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/lim-b/start",
        params={"limit_key": ["db"], "enforce_limits": "true"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_concurrency_limit_multi_key_all_or_nothing(client: AsyncClient):
    """A start under several keys is denied if ANY key is at capacity, and
    acquires no slot at all (no partial acquisition)."""
    await client.put("/api/v1/concurrency-limits/k1", json={"max_concurrent": 1})
    await client.put("/api/v1/concurrency-limits/k2", json={"max_concurrent": 1})
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    for tid in ("mk-a", "mk-b", "mk-c"):
        await client.post(
            f"/api/v1/builds/{build_id}/tasks", json=_register_payload(tid)
        )

    # a holds k1; b wants k1+k2 → denied on k1, must NOT occupy k2.
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/mk-a/start",
        params={"limit_key": ["k1"], "enforce_limits": "true"},
    )
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/mk-b/start",
        params={"limit_key": ["k1", "k2"], "enforce_limits": "true"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["denied_keys"] == ["k1"]

    # k2 must still be free for c.
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/mk-c/start",
        params={"limit_key": ["k2"], "enforce_limits": "true"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_non_enforced_start_records_slots_counted_later(client: AsyncClient):
    """limit_key without enforce_limits still records slot rows — a later
    ENFORCED start must count them as occupied."""
    await client.put("/api/v1/concurrency-limits/soft", json={"max_concurrent": 1})
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    for tid in ("soft-a", "soft-b"):
        await client.post(
            f"/api/v1/builds/{build_id}/tasks", json=_register_payload(tid)
        )

    # Unenforced start (e.g. a resident build tagging keys informationally).
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/soft-a/start",
        params={"limit_key": ["soft"]},
    )
    assert response.status_code == 200

    # Enforced start sees the slot occupied.
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/soft-b/start",
        params={"limit_key": ["soft"], "enforce_limits": "true"},
    )
    assert response.status_code == 409


async def _race_enforced_starts(client: AsyncClient) -> list[int]:
    """Fire 6 concurrent enforced starts against a 2-slot key; return codes."""
    import asyncio as _asyncio

    await client.put("/api/v1/concurrency-limits/race", json={"max_concurrent": 2})
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    task_ids = [f"race-{i}" for i in range(6)]
    for tid in task_ids:
        await client.post(
            f"/api/v1/builds/{build_id}/tasks", json=_register_payload(tid)
        )
    responses = await _asyncio.gather(
        *[
            client.post(
                f"/api/v1/builds/{build_id}/tasks/{tid}/start",
                params={"limit_key": ["race"], "enforce_limits": "true"},
            )
            for tid in task_ids
        ]
    )
    return sorted(r.status_code for r in responses)


@pytest.mark.asyncio
async def test_concurrent_enforced_starts_no_errors(client: AsyncClient):
    """Concurrent enforced starts never 500 (duplicate slot-row races are
    absorbed by ON CONFLICT DO NOTHING). The cap itself isn't asserted here:
    SQLite's with_for_update() is a no-op, so the check-then-commit window
    interleaves — see the Postgres test below for the serialization
    guarantee."""
    statuses = await _race_enforced_starts(client)
    assert set(statuses) <= {200, 409}


@pytest.mark.asyncio
async def test_concurrent_enforced_starts_respect_cap_postgres(pg_client):
    """The FOR UPDATE serialization guarantee: concurrent enforced starts
    never exceed the cap (exactly 2 of 6 acquire a 2-slot key). Runs on
    the Postgres tier (skipped unless STARDAG_API_TEST_DATABASE_URL is
    set — the Postgres CI job provides it)."""
    statuses = await _race_enforced_starts(pg_client)
    assert statuses == [200, 200, 409, 409, 409, 409]


@pytest.mark.asyncio
async def test_skip_blocked_transitive_chain_and_diamond(client: AsyncClient):
    """skip-blocked marks pending/suspended tasks transitively downstream of
    a failure as skipped; unrelated/terminal tasks are untouched."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    # chain: bad -> mid -> top ; diamond: bad -> d1, ok -> d1
    # unrelated: free (pending), done (completed)
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=_register_payload("bad"))
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=_register_payload("ok"))
    await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json=_register_payload("mid", deps=["bad"]),
    )
    await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json=_register_payload("top", deps=["mid"]),
    )
    await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json=_register_payload("d1", deps=["bad", "ok"]),
    )
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("free")
    )
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("done")
    )
    await client.post(f"/api/v1/builds/{build_id}/tasks/done/start")
    await client.post(f"/api/v1/builds/{build_id}/tasks/done/complete")
    # mid is SUSPENDED (also skippable), bad FAILED
    await client.post(f"/api/v1/builds/{build_id}/tasks/mid/start")
    await client.post(f"/api/v1/builds/{build_id}/tasks/mid/suspend")
    await client.post(f"/api/v1/builds/{build_id}/tasks/bad/start")
    await client.post(f"/api/v1/builds/{build_id}/tasks/bad/fail")

    response = await client.post(f"/api/v1/builds/{build_id}/skip-blocked")
    assert response.status_code == 200
    assert sorted(response.json()["skipped_task_ids"]) == ["d1", "mid", "top"]

    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert frontier["status_counts"] == {
        "failed": 1,
        "skipped": 3,
        "pending": 2,  # ok + free untouched
        "completed": 1,
    }

    # Idempotent: nothing left to skip.
    response = await client.post(f"/api/v1/builds/{build_id}/skip-blocked")
    assert response.json()["skipped_task_ids"] == []


@pytest.mark.asyncio
async def test_skip_blocked_noop_without_failures(client: AsyncClient):
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=_register_payload("t"))
    response = await client.post(f"/api/v1/builds/{build_id}/skip-blocked")
    assert response.status_code == 200
    assert response.json()["skipped_task_ids"] == []


@pytest.mark.asyncio
async def test_skip_blocked_quota_check_covers_batch(client: AsyncClient):
    """skip-blocked can emit many TASK_SKIPPED events in one call; the 24h
    event-quota check must be performed with the full batch size, not the
    default single-event amount (which would let a batch overshoot the
    remaining quota)."""
    from unittest.mock import patch

    from stardag_api.limits import check_entity_creation_limit as real_check

    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=_register_payload("bad"))
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("mid", deps=["bad"])
    )
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("top", deps=["mid"])
    )
    await client.post(f"/api/v1/builds/{build_id}/tasks/bad/start")
    await client.post(f"/api/v1/builds/{build_id}/tasks/bad/fail")

    checked: list[tuple[str, int]] = []

    async def spy(db, workspace_id, entity_type, settings, amount=1):
        checked.append((entity_type, amount))
        return await real_check(db, workspace_id, entity_type, settings, amount=amount)

    with patch("stardag_api.routes.builds.check_entity_creation_limit", spy):
        response = await client.post(f"/api/v1/builds/{build_id}/skip-blocked")

    assert response.status_code == 200
    assert sorted(response.json()["skipped_task_ids"]) == ["mid", "top"]
    assert checked == [("events", 2)]


@pytest.mark.asyncio
async def test_skip_blocked_cancelled_seed(client: AsyncClient):
    """Cancelled tasks seed the closure the same way failed ones do."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=_register_payload("c"))
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("down", deps=["c"])
    )
    await client.post(f"/api/v1/builds/{build_id}/tasks/c/start")
    await client.post(f"/api/v1/builds/{build_id}/tasks/c/cancel")

    response = await client.post(f"/api/v1/builds/{build_id}/skip-blocked")
    assert response.status_code == 200
    assert response.json()["skipped_task_ids"] == ["down"]


@pytest.mark.asyncio
async def test_skip_blocked_stops_at_completed_intermediate(client: AsyncClient):
    """Blockage does not propagate through a COMPLETED intermediate: its
    downstream is satisfied regardless of the intermediate's own upstreams
    (mirrors the resident engine, which only propagates skips through tasks
    that themselves become skipped)."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=_register_payload("bad"))
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("mid", deps=["bad"])
    )
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("top", deps=["mid"])
    )
    # mid completed (e.g. in an earlier build) despite bad failing now.
    await client.post(f"/api/v1/builds/{build_id}/tasks/mid/start")
    await client.post(f"/api/v1/builds/{build_id}/tasks/mid/complete")
    await client.post(f"/api/v1/builds/{build_id}/tasks/bad/start")
    await client.post(f"/api/v1/builds/{build_id}/tasks/bad/fail")

    response = await client.post(f"/api/v1/builds/{build_id}/skip-blocked")
    assert response.status_code == 200
    assert response.json()["skipped_task_ids"] == []  # top stays runnable


@pytest.mark.asyncio
async def test_skip_blocked_visible_to_other_builds(client: AsyncClient):
    """Task status is env-scoped: a task skipped via build A's closure drops
    out of build B's actionable frontier (loud, not silent — B hits the
    blocked terminal and a re-trigger resets it via retry)."""
    build_a = (await client.post("/api/v1/builds", json={})).json()["id"]
    build_b = (await client.post("/api/v1/builds", json={})).json()["id"]
    for bid in (build_a, build_b):
        await client.post(f"/api/v1/builds/{bid}/tasks", json=_register_payload("dep"))
        await client.post(
            f"/api/v1/builds/{bid}/tasks",
            json=_register_payload("shared", deps=["dep"]),
        )
    await client.post(f"/api/v1/builds/{build_a}/tasks/dep/start")
    await client.post(f"/api/v1/builds/{build_a}/tasks/dep/fail")

    response = await client.post(f"/api/v1/builds/{build_a}/skip-blocked")
    assert response.json()["skipped_task_ids"] == ["shared"]

    frontier_b = (await client.get(f"/api/v1/builds/{build_b}/frontier")).json()
    actionable_ids = [t["task_id"] for t in frontier_b["actionable"]]
    assert "shared" not in actionable_ids
    assert frontier_b["status_counts"].get("skipped") == 1


# ---------------------------------------------------------------------------
# Executor metadata (task + build level)
# ---------------------------------------------------------------------------

_MODAL_METADATA = {
    "kind": "modal",
    "app_name": "demo-app",
    "workspace": "acme",
    "environment": "prod",
    "function_name": "worker_default",
}


def _metadata_param(metadata: dict) -> str:
    import json as _json

    return _json.dumps(metadata)


@pytest.mark.asyncio
async def test_task_start_executor_metadata_round_trip(client: AsyncClient):
    """executor_metadata on /start lands in the event metadata and is
    denormalised to every response surface that carries executor fields."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("meta-t")
    )
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/meta-t/start",
        params={
            "executor": "modal",
            "executor_ref": "fc-meta-1",
            "executor_metadata": _metadata_param(_MODAL_METADATA),
        },
    )
    assert response.status_code == 200

    # Task detail (GET /tasks/{task_id}).
    task = (await client.get("/api/v1/tasks/meta-t")).json()
    assert task["latest_executor"] == "modal"
    assert task["latest_executor_ref"] == "fc-meta-1"
    assert task["latest_executor_metadata"] == _MODAL_METADATA

    # Task rows in the build.
    rows = (await client.get(f"/api/v1/builds/{build_id}/tasks")).json()
    assert rows[0]["latest_executor_metadata"] == _MODAL_METADATA

    # Frontier refs.
    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert frontier["running"][0]["latest_executor_metadata"] == _MODAL_METADATA

    # Bulk-register slim refs (re-register from another build).
    build_b = (await client.post("/api/v1/builds", json={})).json()["id"]
    response = await client.post(
        f"/api/v1/builds/{build_b}/tasks/bulk?id_only=true",
        json={"tasks": [_register_payload("meta-t")]},
    )
    assert response.json()["tasks"][0]["latest_executor_metadata"] == _MODAL_METADATA

    # Raw event metadata.
    events = (await client.get("/api/v1/tasks/meta-t/events")).json()
    started = [e for e in events if e["event_type"] == "task_started"]
    assert started[0]["event_metadata"]["executor_metadata"] == _MODAL_METADATA


@pytest.mark.asyncio
async def test_task_start_executor_metadata_cleared_and_replaced(client: AsyncClient):
    """Metadata follows the executor-ref semantics exactly: replaced on every
    start (cleared by a start without it), cleared on retry."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("meta-clear")
    )
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/meta-clear/start",
        params={
            "executor": "modal",
            "executor_ref": "fc-1",
            "executor_metadata": _metadata_param(_MODAL_METADATA),
        },
    )

    # A later start without metadata clears it (stale-metadata guard).
    await client.post(f"/api/v1/builds/{build_id}/tasks/meta-clear/start")
    task = (await client.get("/api/v1/tasks/meta-clear")).json()
    assert task["latest_executor_metadata"] is None

    # Replace with a different dict.
    replacement = {"kind": "modal", "app_name": "other-app"}
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/meta-clear/start",
        params={"executor_metadata": _metadata_param(replacement)},
    )
    task = (await client.get("/api/v1/tasks/meta-clear")).json()
    assert task["latest_executor_metadata"] == replacement

    # Retry clears it together with the executor ref.
    await client.post(f"/api/v1/builds/{build_id}/tasks/meta-clear/fail")
    await client.post(f"/api/v1/builds/{build_id}/tasks/meta-clear/retry")
    task = (await client.get("/api/v1/tasks/meta-clear")).json()
    assert task["latest_executor_metadata"] is None


@pytest.mark.asyncio
async def test_task_start_executor_metadata_invalid_json_422(client: AsyncClient):
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("meta-bad")
    )
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/meta-bad/start",
        params={"executor_metadata": "{not json"},
    )
    assert response.status_code == 422
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/meta-bad/start",
        params={"executor_metadata": '["not", "an", "object"]'},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_build_executor_metadata_create_and_resume(client: AsyncClient):
    """Build-level metadata: recorded at create, kept on plain resume,
    replaced by a resume that carries new metadata."""
    trigger_metadata = {**_MODAL_METADATA, "function_name": "build", "reactive": False}
    response = await client.post(
        "/api/v1/builds", json={"executor_metadata": trigger_metadata}
    )
    build = response.json()
    build_id = build["id"]
    assert build["executor_metadata"] == trigger_metadata

    # Some activity so /resume records a BUILD_RESUMED event.
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("bm-t")
    )

    # Plain resume (e.g. from inside the Modal build container) keeps the
    # stored trigger metadata.
    resumed = (await client.post(f"/api/v1/builds/{build_id}/resume")).json()
    assert resumed["executor_metadata"] == trigger_metadata

    # A re-trigger with new metadata replaces it.
    retrigger_metadata = {**trigger_metadata, "function_name": "tick", "reactive": True}
    resumed = (
        await client.post(
            f"/api/v1/builds/{build_id}/resume",
            params={"executor_metadata": _metadata_param(retrigger_metadata)},
        )
    ).json()
    assert resumed["executor_metadata"] == retrigger_metadata

    # And it sticks on subsequent reads (list + detail).
    build = (await client.get(f"/api/v1/builds/{build_id}")).json()
    assert build["executor_metadata"] == retrigger_metadata
    listed = (await client.get("/api/v1/builds")).json()["builds"]
    assert [b["executor_metadata"] for b in listed if b["id"] == build_id] == [
        retrigger_metadata
    ]


@pytest.mark.asyncio
async def test_build_without_executor_metadata_defaults_null(client: AsyncClient):
    build = (await client.post("/api/v1/builds", json={})).json()
    assert build["executor_metadata"] is None


# ---------------------------------------------------------------------------
# Concurrency-limit admin: holders + evict
# ---------------------------------------------------------------------------


async def _start_holder(
    client: AsyncClient, build_id: str, task_id: str, keys: list[str]
) -> None:
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload(task_id)
    )
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/{task_id}/start",
        params={
            "limit_key": keys,
            "executor": "modal",
            "executor_ref": f"fc-{task_id}",
            "executor_metadata": _metadata_param(_MODAL_METADATA),
        },
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_concurrency_limit_holders_join(client: AsyncClient):
    """Holders = RUNNING tasks with the key recorded — multi-key tasks show
    under every key they hold; non-RUNNING tasks are excluded."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await _start_holder(client, build_id, "hold-a", ["gpu"])
    await _start_holder(client, build_id, "hold-b", ["gpu", "db"])
    await _start_holder(client, build_id, "hold-c", ["db"])
    # A completed task frees its slots — must not show as a holder.
    await client.post(f"/api/v1/builds/{build_id}/tasks/hold-c/complete")

    gpu = (await client.get("/api/v1/concurrency-limits/gpu/holders")).json()
    assert gpu["key"] == "gpu"
    assert gpu["total"] == 2
    assert [h["task_id"] for h in gpu["holders"]] == ["hold-a", "hold-b"]
    holder = gpu["holders"][0]
    assert holder["task_name"] == "T"
    assert holder["latest_status_at"] is not None
    assert holder["latest_executor"] == "modal"
    assert holder["latest_executor_ref"] == "fc-hold-a"
    assert holder["latest_executor_metadata"] == _MODAL_METADATA

    db_holders = (await client.get("/api/v1/concurrency-limits/db/holders")).json()
    assert [h["task_id"] for h in db_holders["holders"]] == ["hold-b"]

    # Unknown / unconfigured key → empty, not 404 (slots can exist for
    # keys without a configured cap).
    empty = (await client.get("/api/v1/concurrency-limits/nope/holders")).json()
    assert empty == {"key": "nope", "holders": [], "total": 0}


@pytest.mark.asyncio
async def test_concurrency_limit_holders_limit_param(client: AsyncClient):
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    for i in range(3):
        await _start_holder(client, build_id, f"page-{i}", ["paged"])

    response = await client.get(
        "/api/v1/concurrency-limits/paged/holders", params={"limit": 2}
    )
    body = response.json()
    assert body["total"] == 3
    assert len(body["holders"]) == 2
    # Oldest running first (eviction candidates on top).
    assert [h["task_id"] for h in body["holders"]] == ["page-0", "page-1"]


@pytest.mark.asyncio
async def test_evict_holder_frees_slot(client: AsyncClient):
    """Evicting records TASK_FAILED (with the evictor identity) and frees
    the slot: a subsequent enforced start succeeds."""
    await client.put("/api/v1/concurrency-limits/ev", json={"max_concurrent": 1})
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await _start_holder(client, build_id, "ev-a", ["ev"])
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("ev-b")
    )

    # Slot occupied → enforced start denied.
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/ev-b/start",
        params={"limit_key": ["ev"], "enforce_limits": "true"},
    )
    assert response.status_code == 409

    response = await client.post("/api/v1/concurrency-limits/ev/holders/ev-a/evict")
    assert response.status_code == 200
    assert response.json() == {
        "task_id": "ev-a",
        "status": "failed",
        "latest_status": "failed",
    }

    # Failure recorded with the evictor identity (mocked auth user).
    events = (await client.get("/api/v1/tasks/ev-a/events")).json()
    failed = [e for e in events if e["event_type"] == "task_failed"]
    assert len(failed) == 1
    assert "default@localhost" in failed[0]["error_message"]
    assert "'ev'" in failed[0]["error_message"]
    assert failed[0]["event_metadata"]["evicted_by_user_id"] == "default-local-user"
    assert failed[0]["event_metadata"]["concurrency_limit_key"] == "ev"

    # Slot freed → the denied task can start now.
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/ev-b/start",
        params={"limit_key": ["ev"], "enforce_limits": "true"},
    )
    assert response.status_code == 200

    # The evicted task no longer shows as a holder.
    holders = (await client.get("/api/v1/concurrency-limits/ev/holders")).json()
    assert [h["task_id"] for h in holders["holders"]] == ["ev-b"]


@pytest.mark.asyncio
async def test_evict_holder_frees_all_keys(client: AsyncClient):
    """Evicting via ONE key frees ALL the task's slots (normal status
    transition semantics)."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await _start_holder(client, build_id, "multi-ev", ["k-one", "k-two"])

    await client.post("/api/v1/concurrency-limits/k-one/holders/multi-ev/evict")

    for key in ("k-one", "k-two"):
        holders = (await client.get(f"/api/v1/concurrency-limits/{key}/holders")).json()
        assert holders["holders"] == []


@pytest.mark.asyncio
async def test_evict_holder_404_paths(client: AsyncClient):
    """Evict is scoped to CURRENT holders of the key — not a generic
    kill-any-task endpoint."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await _start_holder(client, build_id, "n-holder", ["real-key"])
    # A RUNNING task without the key recorded.
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("n-nokey")
    )
    await client.post(f"/api/v1/builds/{build_id}/tasks/n-nokey/start")
    # A task holding the key but no longer RUNNING.
    await _start_holder(client, build_id, "n-done", ["real-key"])
    await client.post(f"/api/v1/builds/{build_id}/tasks/n-done/complete")

    # Unknown task id.
    response = await client.post(
        "/api/v1/concurrency-limits/real-key/holders/no-such-task/evict"
    )
    assert response.status_code == 404
    # RUNNING but doesn't hold the key.
    response = await client.post(
        "/api/v1/concurrency-limits/real-key/holders/n-nokey/evict"
    )
    assert response.status_code == 404
    # Holds the key but not RUNNING.
    response = await client.post(
        "/api/v1/concurrency-limits/real-key/holders/n-done/evict"
    )
    assert response.status_code == 404
    # Right task, wrong key.
    response = await client.post(
        "/api/v1/concurrency-limits/other-key/holders/n-holder/evict"
    )
    assert response.status_code == 404
    # The real holder is untouched by all of the above.
    holders = (await client.get("/api/v1/concurrency-limits/real-key/holders")).json()
    assert [h["task_id"] for h in holders["holders"]] == ["n-holder"]


@pytest.mark.asyncio
async def test_limits_admin_requires_auth(unauthenticated_client: AsyncClient):
    response = await unauthenticated_client.get(
        "/api/v1/concurrency-limits/gpu/holders"
    )
    assert response.status_code == 401
    response = await unauthenticated_client.post(
        "/api/v1/concurrency-limits/gpu/holders/some-task/evict"
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Executor metadata size cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_metadata_size_cap_all_paths(client: AsyncClient):
    """Oversized executor_metadata (> 2 KB compact JSON) is a 422 on every
    ingest path: task start (query param), build create (body), build
    resume (query param)."""
    oversized = {"kind": "modal", "blob": "x" * 3000}

    # Build-create body path.
    response = await client.post(
        "/api/v1/builds", json={"executor_metadata": oversized}
    )
    assert response.status_code == 422
    assert "executor_metadata" in str(response.json()["detail"])

    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("cap-t")
    )

    # Task-start query-param path.
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/cap-t/start",
        params={"executor_metadata": _metadata_param(oversized)},
    )
    assert response.status_code == 422

    # Build-resume query-param path.
    response = await client.post(
        f"/api/v1/builds/{build_id}/resume",
        params={"executor_metadata": _metadata_param(oversized)},
    )
    assert response.status_code == 422

    # A dict within the cap passes on all three paths.
    ok = dict(_MODAL_METADATA)
    assert (
        await client.post("/api/v1/builds", json={"executor_metadata": ok})
    ).status_code == 201
    assert (
        await client.post(
            f"/api/v1/builds/{build_id}/tasks/cap-t/start",
            params={"executor_metadata": _metadata_param(ok)},
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/builds/{build_id}/resume",
            params={"executor_metadata": _metadata_param(ok)},
        )
    ).status_code == 200


# ---------------------------------------------------------------------------
# Evict: build wake-up, evicted-then-completes, tenancy, admin gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evict_sets_owning_build_needs_tick(client: AsyncClient):
    """Eviction sets the owning build's scheduler wake-up flag in the same
    transaction, so a reactive build observes it on the next tick instead
    of the next watchdog sweep."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await _start_holder(client, build_id, "wake-ev", ["wake"])

    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert frontier["needs_tick"] is False

    response = await client.post(
        "/api/v1/concurrency-limits/wake/holders/wake-ev/evict"
    )
    assert response.status_code == 200

    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert frontier["needs_tick"] is True


@pytest.mark.asyncio
async def test_evicted_then_worker_completes_sticky_completed(client: AsyncClient):
    """Evicting a holder whose worker is actually alive: the worker's later
    TASK_COMPLETED flips the task COMPLETED (sticky) — the documented
    consequence of evicting a live execution."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await _start_holder(client, build_id, "alive-ev", ["alive"])

    response = await client.post(
        "/api/v1/concurrency-limits/alive/holders/alive-ev/evict"
    )
    assert response.json() == {
        "task_id": "alive-ev",
        "status": "failed",
        "latest_status": "failed",
    }

    # The (still-alive) worker reports completion afterwards.
    response = await client.post(f"/api/v1/builds/{build_id}/tasks/alive-ev/complete")
    assert response.status_code == 200
    assert response.json()["status"] == "completed"

    # Sticky-completed wins over the eviction failure and stays.
    frontier = (await client.get(f"/api/v1/builds/{build_id}/frontier")).json()
    assert frontier["status_counts"] == {"completed": 1}
    # Completed → not a holder either.
    holders = (await client.get("/api/v1/concurrency-limits/alive/holders")).json()
    assert holders["holders"] == []


@pytest.mark.asyncio
async def test_holders_and_evict_environment_isolation(
    client: AsyncClient, as_environment_b
):
    """Holders and evict are environment-scoped: another environment's auth
    (same workspace, same user) sees no holders for the key and cannot
    evict them."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await _start_holder(client, build_id, "iso-holder", ["iso"])

    holders = (await client.get("/api/v1/concurrency-limits/iso/holders")).json()
    assert [h["task_id"] for h in holders["holders"]] == ["iso-holder"]

    with as_environment_b():
        holders_b = (await client.get("/api/v1/concurrency-limits/iso/holders")).json()
        assert holders_b == {"key": "iso", "holders": [], "total": 0}
        response = await client.post(
            "/api/v1/concurrency-limits/iso/holders/iso-holder/evict"
        )
        assert response.status_code == 404

    # The holder in the original environment is untouched.
    holders = (await client.get("/api/v1/concurrency-limits/iso/holders")).json()
    assert [h["task_id"] for h in holders["holders"]] == ["iso-holder"]
    assert (await client.get("/api/v1/tasks/iso-holder")).json()[
        "latest_executor"
    ] == "modal"


# ---------------------------------------------------------------------------
# Concurrency-limit writes: admin gate on the user (JWT) auth path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_limit_writes_admin_gated_for_users(
    client: AsyncClient, role_auth_switcher
):
    """PUT/DELETE /concurrency-limits/{key} and evict require the workspace
    ADMIN role on the user (JWT) auth path; reads stay member-level."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await _start_holder(client, build_id, "gate-holder", ["gate"])

    # Admin (default client user is workspace OWNER): all writes allowed.
    assert (
        await client.put("/api/v1/concurrency-limits/gate", json={"max_concurrent": 2})
    ).status_code == 200

    with role_auth_switcher["member"]():
        # Reads stay member-level.
        assert (await client.get("/api/v1/concurrency-limits")).status_code == 200
        holders = await client.get("/api/v1/concurrency-limits/gate/holders")
        assert holders.status_code == 200
        # Writes are 403 for members.
        response = await client.put(
            "/api/v1/concurrency-limits/gate", json={"max_concurrent": 3}
        )
        assert response.status_code == 403
        assert "admin" in response.json()["detail"].lower()
        assert (
            await client.delete("/api/v1/concurrency-limits/gate")
        ).status_code == 403
        assert (
            await client.post(
                "/api/v1/concurrency-limits/gate/holders/gate-holder/evict"
            )
        ).status_code == 403

    # Member 403s changed nothing.
    limits = (await client.get("/api/v1/concurrency-limits")).json()["limits"]
    assert limits == [{"key": "gate", "max_concurrent": 2}]

    # API-key auth (machine credential): full access.
    with role_auth_switcher["api_key"]():
        assert (
            await client.put(
                "/api/v1/concurrency-limits/gate", json={"max_concurrent": 5}
            )
        ).status_code == 200
        assert (
            await client.post(
                "/api/v1/concurrency-limits/gate/holders/gate-holder/evict"
            )
        ).status_code == 200
        assert (
            await client.delete("/api/v1/concurrency-limits/gate")
        ).status_code == 204


@pytest.mark.asyncio
async def test_evict_admin_allowed_and_records_admin_identity(client: AsyncClient):
    """The default (OWNER) user can evict — pinned separately so the admin
    gate can't silently lock admins out."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await _start_holder(client, build_id, "admin-ev", ["adm"])

    response = await client.post(
        "/api/v1/concurrency-limits/adm/holders/admin-ev/evict"
    )
    assert response.status_code == 200
    assert response.json() == {
        "task_id": "admin-ev",
        "status": "failed",
        "latest_status": "failed",
    }


@pytest.mark.asyncio
async def test_claim_start_denies_running_and_echoes_ref(client: AsyncClient):
    """A claiming start loses to a RUNNING task and gets the running
    execution's ref back (for re-attach); a plain start is unaffected."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("claim-a")
    )

    # First claimant wins.
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/claim-a/start",
        params={
            "claim": "true",
            "executor": "modal",
            "executor_ref": "fc-winner",
        },
    )
    assert response.status_code == 200

    # Second claimant is denied with the winner's ref echoed.
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/claim-a/start",
        params={"claim": "true"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error_code"] == "task_already_running"
    assert detail["executor"] == "modal"
    assert detail["executor_ref"] == "fc-winner"

    # A non-claiming start (e.g. the winner recording a new ref, or an old
    # client) is still allowed — duplicate starts remain tolerated.
    response = await client.post(f"/api/v1/builds/{build_id}/tasks/claim-a/start")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_claim_start_denies_completed(client: AsyncClient):
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("claim-done")
    )
    await client.post(f"/api/v1/builds/{build_id}/tasks/claim-done/start")
    await client.post(f"/api/v1/builds/{build_id}/tasks/claim-done/complete")

    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/claim-done/start",
        params={"claim": "true"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "task_already_completed"


@pytest.mark.asyncio
async def test_claim_start_allowed_after_failure_and_suspension(client: AsyncClient):
    """FAILED and SUSPENDED tasks are claimable (retry / dynamic-deps
    re-invocation) — only RUNNING and COMPLETED block."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register_payload("claim-retry")
    )

    await client.post(f"/api/v1/builds/{build_id}/tasks/claim-retry/start")
    await client.post(f"/api/v1/builds/{build_id}/tasks/claim-retry/fail")
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/claim-retry/start",
        params={"claim": "true"},
    )
    assert response.status_code == 200  # failed → claimable again

    await client.post(f"/api/v1/builds/{build_id}/tasks/claim-retry/suspend")
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/claim-retry/start",
        params={"claim": "true"},
    )
    assert response.status_code == 200  # suspended → claimable (re-invocation)


@pytest.mark.asyncio
async def test_denied_claim_consumes_no_limit_slot(client: AsyncClient):
    """A start that passes the limits pre-check but loses the claim rolls
    the whole transaction back — no event, no limit-key rows occupied."""
    await client.put("/api/v1/concurrency-limits/cl-k", json={"max_concurrent": 1})
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    for tid in ("cl-a", "cl-b", "cl-c"):
        await client.post(
            f"/api/v1/builds/{build_id}/tasks", json=_register_payload(tid)
        )

    # cl-a runs WITHOUT the limit key, claiming.
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/cl-a/start", params={"claim": "true"}
    )
    # cl-a again with claim + limit key: denied by CLAIM (already running) —
    # must not have consumed the cl-k slot despite passing the limits check.
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/cl-a/start",
        params={"claim": "true", "limit_key": ["cl-k"], "enforce_limits": "true"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "task_already_running"

    # The cl-k slot is still free: cl-b acquires it.
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/cl-b/start",
        params={"claim": "true", "limit_key": ["cl-k"], "enforce_limits": "true"},
    )
    assert response.status_code == 200
    # And now the slot is genuinely occupied: cl-c is limit-denied.
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/cl-c/start",
        params={"claim": "true", "limit_key": ["cl-k"], "enforce_limits": "true"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "concurrency_limit_reached"
