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
