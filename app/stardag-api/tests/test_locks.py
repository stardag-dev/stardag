"""Tests for distributed lock endpoints."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import DistributedLock, Environment
from tests.conftest import DEFAULT_ENVIRONMENT_ID


@pytest.mark.asyncio
async def test_acquire_lock(client: AsyncClient):
    """Test acquiring a new lock."""
    owner_id = str(uuid.uuid4())
    response = await client.post(
        "/api/v1/locks/test-task-id/acquire",
        json={
            "owner_id": owner_id,
            "ttl_seconds": 60,
            "check_task_completion": False,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "acquired"
    assert data["acquired"] is True
    assert data["lock"] is not None
    assert data["lock"]["name"] == "test-task-id"
    assert data["lock"]["owner_id"] == owner_id


@pytest.mark.asyncio
async def test_acquire_lock_reentrant(client: AsyncClient):
    """Test re-acquiring a lock with the same owner (re-entrant)."""
    owner_id = str(uuid.uuid4())

    # First acquisition
    response = await client.post(
        "/api/v1/locks/reentrant-task/acquire",
        json={"owner_id": owner_id, "ttl_seconds": 60, "check_task_completion": False},
    )
    assert response.status_code == 200
    assert response.json()["acquired"] is True
    version1 = response.json()["lock"]["version"]

    # Second acquisition with same owner should succeed (re-entrant)
    response = await client.post(
        "/api/v1/locks/reentrant-task/acquire",
        json={"owner_id": owner_id, "ttl_seconds": 60, "check_task_completion": False},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["acquired"] is True
    # Version should be incremented
    assert data["lock"]["version"] == version1 + 1


@pytest.mark.asyncio
async def test_acquire_lock_held_by_other(client: AsyncClient):
    """Test that acquiring a lock held by another owner returns 423."""
    owner1 = str(uuid.uuid4())
    owner2 = str(uuid.uuid4())

    # First owner acquires
    response = await client.post(
        "/api/v1/locks/contested-task/acquire",
        json={"owner_id": owner1, "ttl_seconds": 300, "check_task_completion": False},
    )
    assert response.status_code == 200
    assert response.json()["acquired"] is True

    # Second owner tries to acquire - should fail
    response = await client.post(
        "/api/v1/locks/contested-task/acquire",
        json={"owner_id": owner2, "ttl_seconds": 60, "check_task_completion": False},
    )
    assert response.status_code == 423
    data = response.json()
    assert data["detail"]["status"] == "held_by_other"


@pytest.mark.asyncio
async def test_acquire_expired_lock(client: AsyncClient, async_session: AsyncSession):
    """Test acquiring a lock that has expired."""
    owner1 = str(uuid.uuid4())
    owner2 = str(uuid.uuid4())

    # First owner acquires
    response = await client.post(
        "/api/v1/locks/expiring-task/acquire",
        json={"owner_id": owner1, "ttl_seconds": 60, "check_task_completion": False},
    )
    assert response.status_code == 200
    assert response.json()["acquired"] is True

    # Manually expire the lock
    await async_session.execute(
        update(DistributedLock)
        .where(DistributedLock.name == "expiring-task")
        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=10))
    )
    await async_session.commit()

    # Second owner should be able to acquire the expired lock
    response = await client.post(
        "/api/v1/locks/expiring-task/acquire",
        json={"owner_id": owner2, "ttl_seconds": 60, "check_task_completion": False},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["acquired"] is True
    assert data["lock"]["owner_id"] == owner2


@pytest.mark.asyncio
async def test_acquire_lock_already_completed(client: AsyncClient):
    """Test that acquiring a lock for a completed task returns already_completed."""
    owner_id = str(uuid.uuid4())

    # Create a build and complete a task
    build_response = await client.post("/api/v1/builds", json={})
    build_id = build_response.json()["id"]

    task_data = {
        "task_id": "completed-task-id",
        "task_namespace": "",
        "task_name": "TestTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    await client.post(f"/api/v1/builds/{build_id}/tasks/completed-task-id/start")
    await client.post(f"/api/v1/builds/{build_id}/tasks/completed-task-id/complete")

    # Try to acquire lock with check_task_completion=True
    response = await client.post(
        "/api/v1/locks/completed-task-id/acquire",
        json={"owner_id": owner_id, "ttl_seconds": 60, "check_task_completion": True},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "already_completed"
    assert data["acquired"] is False


@pytest.mark.asyncio
async def test_renew_lock(client: AsyncClient):
    """Test renewing a lock's TTL."""
    owner_id = str(uuid.uuid4())

    # Acquire lock
    response = await client.post(
        "/api/v1/locks/renew-task/acquire",
        json={"owner_id": owner_id, "ttl_seconds": 30, "check_task_completion": False},
    )
    assert response.status_code == 200
    original_expires_at = response.json()["lock"]["expires_at"]

    # Renew with longer TTL
    response = await client.post(
        "/api/v1/locks/renew-task/renew",
        json={"owner_id": owner_id, "ttl_seconds": 120},
    )
    assert response.status_code == 200
    assert response.json()["renewed"] is True

    # Verify expiration was extended
    response = await client.get("/api/v1/locks/renew-task")
    assert response.status_code == 200
    new_expires_at = response.json()["expires_at"]
    assert new_expires_at > original_expires_at


@pytest.mark.asyncio
async def test_renew_lock_not_owner(client: AsyncClient):
    """Test that renewing a lock as non-owner fails."""
    owner1 = str(uuid.uuid4())
    owner2 = str(uuid.uuid4())

    # Owner 1 acquires
    response = await client.post(
        "/api/v1/locks/renew-not-owner/acquire",
        json={"owner_id": owner1, "ttl_seconds": 60, "check_task_completion": False},
    )
    assert response.status_code == 200

    # Owner 2 tries to renew - should fail
    response = await client.post(
        "/api/v1/locks/renew-not-owner/renew",
        json={"owner_id": owner2, "ttl_seconds": 120},
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_renew_nonexistent_lock(client: AsyncClient):
    """Test that renewing a non-existent lock fails."""
    owner_id = str(uuid.uuid4())

    response = await client.post(
        "/api/v1/locks/nonexistent-lock/renew",
        json={"owner_id": owner_id, "ttl_seconds": 60},
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_release_lock(client: AsyncClient):
    """Test releasing a lock."""
    owner_id = str(uuid.uuid4())

    # Acquire lock
    response = await client.post(
        "/api/v1/locks/release-task/acquire",
        json={"owner_id": owner_id, "ttl_seconds": 60, "check_task_completion": False},
    )
    assert response.status_code == 200

    # Release lock
    response = await client.post(
        "/api/v1/locks/release-task/release",
        json={"owner_id": owner_id, "task_completed": False},
    )
    assert response.status_code == 200
    assert response.json()["released"] is True

    # Verify lock is gone
    response = await client.get("/api/v1/locks/release-task")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_release_lock_not_owner(client: AsyncClient):
    """Test that releasing a lock as non-owner fails."""
    owner1 = str(uuid.uuid4())
    owner2 = str(uuid.uuid4())

    # Owner 1 acquires
    response = await client.post(
        "/api/v1/locks/release-not-owner/acquire",
        json={"owner_id": owner1, "ttl_seconds": 60, "check_task_completion": False},
    )
    assert response.status_code == 200

    # Owner 2 tries to release - should fail
    response = await client.post(
        "/api/v1/locks/release-not-owner/release",
        json={"owner_id": owner2, "task_completed": False},
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_release_lock_with_completion(client: AsyncClient):
    """Test releasing a lock with task completion recording."""
    owner_id = str(uuid.uuid4())

    # Create a build and register a task
    build_response = await client.post("/api/v1/builds", json={})
    build_id = build_response.json()["id"]

    task_data = {
        "task_id": "release-complete-task",
        "task_namespace": "",
        "task_name": "TestTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    await client.post(f"/api/v1/builds/{build_id}/tasks/release-complete-task/start")

    # Acquire lock
    response = await client.post(
        "/api/v1/locks/release-complete-task/acquire",
        json={"owner_id": owner_id, "ttl_seconds": 60, "check_task_completion": False},
    )
    assert response.status_code == 200

    # Release with task_completed=True
    response = await client.post(
        "/api/v1/locks/release-complete-task/release",
        json={
            "owner_id": owner_id,
            "task_completed": True,
            "build_id": build_id,
        },
    )
    assert response.status_code == 200
    assert response.json()["released"] is True

    # Verify task is marked as completed in the build
    response = await client.get(f"/api/v1/builds/{build_id}/tasks")
    tasks = response.json()
    task = next(t for t in tasks if t["task_id"] == "release-complete-task")
    assert task["status"] == "completed"


@pytest.mark.asyncio
async def test_list_locks(client: AsyncClient):
    """Test listing active locks in environment."""
    # Create multiple locks
    for i in range(3):
        owner_id = str(uuid.uuid4())
        await client.post(
            f"/api/v1/locks/list-task-{i}/acquire",
            json={
                "owner_id": owner_id,
                "ttl_seconds": 60,
                "check_task_completion": False,
            },
        )

    # List locks
    response = await client.get("/api/v1/locks")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] >= 3
    lock_names = [lock["name"] for lock in data["locks"]]
    assert "list-task-0" in lock_names
    assert "list-task-1" in lock_names
    assert "list-task-2" in lock_names


@pytest.mark.asyncio
async def test_list_locks_exclude_expired(
    client: AsyncClient, async_session: AsyncSession
):
    """Test that expired locks are excluded from listing by default."""
    owner_id = str(uuid.uuid4())

    # Acquire a lock
    await client.post(
        "/api/v1/locks/expired-list-task/acquire",
        json={"owner_id": owner_id, "ttl_seconds": 60, "check_task_completion": False},
    )

    # Manually expire it
    await async_session.execute(
        update(DistributedLock)
        .where(DistributedLock.name == "expired-list-task")
        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=10))
    )
    await async_session.commit()

    # List should not include expired lock
    response = await client.get("/api/v1/locks")
    assert response.status_code == 200
    lock_names = [lock["name"] for lock in response.json()["locks"]]
    assert "expired-list-task" not in lock_names

    # List with include_expired=True should include it
    response = await client.get("/api/v1/locks", params={"include_expired": True})
    assert response.status_code == 200
    lock_names = [lock["name"] for lock in response.json()["locks"]]
    assert "expired-list-task" in lock_names


@pytest.mark.asyncio
async def test_get_lock(client: AsyncClient):
    """Test getting a specific lock."""
    owner_id = str(uuid.uuid4())

    # Acquire a lock
    response = await client.post(
        "/api/v1/locks/get-task/acquire",
        json={"owner_id": owner_id, "ttl_seconds": 60, "check_task_completion": False},
    )
    assert response.status_code == 200

    # Get the lock
    response = await client.get("/api/v1/locks/get-task")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "get-task"
    assert data["owner_id"] == owner_id


@pytest.mark.asyncio
async def test_get_lock_not_found(client: AsyncClient):
    """Test that getting a non-existent lock returns 404."""
    response = await client.get("/api/v1/locks/nonexistent-task")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_check_task_completion_status_not_completed(client: AsyncClient):
    """Test checking completion status for a task that is not completed."""
    response = await client.get(
        "/api/v1/locks/tasks/uncompleted-task/completion-status"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == "uncompleted-task"
    assert data["is_completed"] is False


@pytest.mark.asyncio
async def test_check_task_completion_status_completed(client: AsyncClient):
    """Test checking completion status for a completed task."""
    # Create a build and complete a task
    build_response = await client.post("/api/v1/builds", json={})
    build_id = build_response.json()["id"]

    task_data = {
        "task_id": "check-completed-task",
        "task_namespace": "",
        "task_name": "TestTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    await client.post(f"/api/v1/builds/{build_id}/tasks/check-completed-task/start")
    await client.post(f"/api/v1/builds/{build_id}/tasks/check-completed-task/complete")

    # Check completion status
    response = await client.get(
        "/api/v1/locks/tasks/check-completed-task/completion-status"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == "check-completed-task"
    assert data["is_completed"] is True


@pytest.mark.asyncio
async def test_environment_concurrency_limit(
    client: AsyncClient, async_session: AsyncSession
):
    """Test that environment concurrency limit is enforced."""
    # Set environment max_concurrent_locks to 2
    await async_session.execute(
        update(Environment)
        .where(Environment.id == DEFAULT_ENVIRONMENT_ID)
        .values(max_concurrent_locks=2)
    )
    await async_session.commit()

    # Acquire 2 locks (should succeed)
    for i in range(2):
        owner_id = str(uuid.uuid4())
        response = await client.post(
            f"/api/v1/locks/limit-task-{i}/acquire",
            json={
                "owner_id": owner_id,
                "ttl_seconds": 60,
                "check_task_completion": False,
            },
        )
        assert response.status_code == 200
        assert response.json()["acquired"] is True

    # Third lock should fail with 429
    owner_id = str(uuid.uuid4())
    response = await client.post(
        "/api/v1/locks/limit-task-3/acquire",
        json={"owner_id": owner_id, "ttl_seconds": 60, "check_task_completion": False},
    )
    assert response.status_code == 429
    data = response.json()
    assert data["detail"]["status"] == "concurrency_limit_reached"

    # Reset limit
    await async_session.execute(
        update(Environment)
        .where(Environment.id == DEFAULT_ENVIRONMENT_ID)
        .values(max_concurrent_locks=None)
    )
    await async_session.commit()


@pytest.mark.asyncio
async def test_environment_concurrency_limit_same_owner_exempt(
    client: AsyncClient, async_session: AsyncSession
):
    """Test that same owner re-acquiring doesn't count against limit."""
    # Set environment max_concurrent_locks to 1
    await async_session.execute(
        update(Environment)
        .where(Environment.id == DEFAULT_ENVIRONMENT_ID)
        .values(max_concurrent_locks=1)
    )
    await async_session.commit()

    owner_id = str(uuid.uuid4())

    # Acquire first lock
    response = await client.post(
        "/api/v1/locks/limit-reentrant-task/acquire",
        json={"owner_id": owner_id, "ttl_seconds": 60, "check_task_completion": False},
    )
    assert response.status_code == 200
    assert response.json()["acquired"] is True

    # Same owner re-acquiring should succeed (doesn't count as new lock)
    response = await client.post(
        "/api/v1/locks/limit-reentrant-task/acquire",
        json={"owner_id": owner_id, "ttl_seconds": 60, "check_task_completion": False},
    )
    assert response.status_code == 200
    assert response.json()["acquired"] is True

    # Reset limit
    await async_session.execute(
        update(Environment)
        .where(Environment.id == DEFAULT_ENVIRONMENT_ID)
        .values(max_concurrent_locks=None)
    )
    await async_session.commit()


@pytest.mark.asyncio
async def test_owner_id_comparison_with_uuid_type(
    client: AsyncClient, async_session: AsyncSession
):
    """Test that owner_id comparisons work correctly with UUID objects.

    This test verifies that the lock service correctly handles UUID/string
    comparisons, which was a bug when SQLite returned strings but PostgreSQL
    returned UUID objects. The comparison should work regardless of the
    underlying database's type representation.
    """
    # Use a proper UUID object (as the API schema now requires)
    owner_uuid = uuid.uuid4()
    owner_id = str(owner_uuid)

    # Acquire a lock
    response = await client.post(
        "/api/v1/locks/uuid-type-test/acquire",
        json={"owner_id": owner_id, "ttl_seconds": 60, "check_task_completion": False},
    )
    assert response.status_code == 200
    assert response.json()["acquired"] is True
    lock_data = response.json()["lock"]

    # Verify the owner_id in response matches what we sent
    assert lock_data["owner_id"] == owner_id

    # Directly query the database and verify comparison works
    result = await async_session.execute(
        select(DistributedLock).where(DistributedLock.name == "uuid-type-test")
    )
    lock = result.scalar_one()

    # The critical test: verify that string comparison of owner_id works
    # This is what caught the bug - SQLite returns str, PostgreSQL returns UUID
    assert str(lock.owner_id) == owner_id

    # Re-acquire with same owner should work (re-entrant)
    response = await client.post(
        "/api/v1/locks/uuid-type-test/acquire",
        json={"owner_id": owner_id, "ttl_seconds": 60, "check_task_completion": False},
    )
    assert response.status_code == 200
    assert response.json()["acquired"] is True

    # Renew should work
    response = await client.post(
        "/api/v1/locks/uuid-type-test/renew",
        json={"owner_id": owner_id, "ttl_seconds": 120},
    )
    assert response.status_code == 200
    assert response.json()["renewed"] is True

    # Different owner should fail
    different_owner = str(uuid.uuid4())
    response = await client.post(
        "/api/v1/locks/uuid-type-test/renew",
        json={"owner_id": different_owner, "ttl_seconds": 120},
    )
    assert response.status_code == 409

    # Release should work with original owner
    response = await client.post(
        "/api/v1/locks/uuid-type-test/release",
        json={"owner_id": owner_id, "task_completed": False},
    )
    assert response.status_code == 200
    assert response.json()["released"] is True
