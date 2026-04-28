"""Tests for the denormalised Task.latest_* columns.

Verifies that:
- apply_event_to_task encodes the same priority logic as the historical
  event-scan helper.
- Lifecycle endpoints (start/complete/fail/cancel/etc.) keep latest_* in
  sync with what an event scan would have computed.
- get_all_task_global_statuses returns equivalent results from the columns.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import Event, EventType, Task, TaskStatus
from stardag_api.models.base import generate_uuid7
from stardag_api.services.status import (
    apply_event_to_task,
    get_all_task_global_statuses,
)
from tests.conftest import DEFAULT_ENVIRONMENT_ID


# ---------------------------------------------------------------------------
# apply_event_to_task — priority + ordering
# ---------------------------------------------------------------------------


def _new_task() -> Task:
    return Task(
        id=generate_uuid7(),
        task_id="t1",
        environment_id=DEFAULT_ENVIRONMENT_ID,
        task_namespace="",
        task_name="t1",
        task_data={},
        is_phantom=False,
        latest_status=TaskStatus.PENDING,
        latest_waiting_for_lock=False,
    )


def _event(
    et: EventType,
    *,
    build_id: UUID | None = None,
    created_at: datetime | None = None,
    error_message: str | None = None,
    metadata: dict | None = None,
) -> Event:
    return Event(
        id=generate_uuid7(),
        build_id=build_id or generate_uuid7(),
        task_id=None,  # not relevant for the helper
        event_type=et,
        created_at=created_at or datetime.now(timezone.utc),
        error_message=error_message,
        event_metadata=metadata,
    )


class TestApplyEventToTask:
    def test_pending_to_running_on_start(self) -> None:
        task = _new_task()
        apply_event_to_task(task, _event(EventType.TASK_STARTED))
        assert task.latest_status == TaskStatus.RUNNING
        assert task.latest_started_at is not None

    def test_running_to_completed(self) -> None:
        task = _new_task()
        apply_event_to_task(task, _event(EventType.TASK_STARTED))
        apply_event_to_task(task, _event(EventType.TASK_COMPLETED))
        assert task.latest_status == TaskStatus.COMPLETED
        assert task.latest_completed_at is not None

    def test_completed_is_sticky(self) -> None:
        task = _new_task()
        apply_event_to_task(task, _event(EventType.TASK_COMPLETED))
        apply_event_to_task(task, _event(EventType.TASK_FAILED))
        apply_event_to_task(task, _event(EventType.TASK_STARTED))
        assert task.latest_status == TaskStatus.COMPLETED

    def test_failed_then_started_goes_back_to_running(self) -> None:
        # Matches the historical retry semantic: RUNNING wins over FAILED.
        task = _new_task()
        apply_event_to_task(task, _event(EventType.TASK_FAILED, error_message="boom"))
        assert task.latest_status == TaskStatus.FAILED
        apply_event_to_task(task, _event(EventType.TASK_STARTED))
        assert task.latest_status == TaskStatus.RUNNING

    def test_pending_to_waiting_for_lock(self) -> None:
        task = _new_task()
        apply_event_to_task(task, _event(EventType.TASK_WAITING_FOR_LOCK))
        assert task.latest_status == TaskStatus.PENDING
        assert task.latest_waiting_for_lock is True

    def test_waiting_for_lock_does_not_set_when_running(self) -> None:
        task = _new_task()
        apply_event_to_task(task, _event(EventType.TASK_STARTED))
        apply_event_to_task(task, _event(EventType.TASK_WAITING_FOR_LOCK))
        assert task.latest_waiting_for_lock is False

    def test_referenced_is_status_neutral(self) -> None:
        task = _new_task()
        apply_event_to_task(task, _event(EventType.TASK_STARTED))
        apply_event_to_task(task, _event(EventType.TASK_REFERENCED))
        assert task.latest_status == TaskStatus.RUNNING

    def test_commit_hash_propagates(self) -> None:
        task = _new_task()
        apply_event_to_task(
            task,
            _event(EventType.TASK_STARTED, metadata={"commit_hash": "abc123"}),
        )
        assert task.latest_commit_hash == "abc123"


# ---------------------------------------------------------------------------
# Lifecycle endpoint integration: lifecycle endpoints update the columns
# ---------------------------------------------------------------------------


class TestLifecycleUpdatesLatestColumns:
    async def _create_build_and_task(
        self, client: AsyncClient, task_id: str
    ) -> tuple[str, str]:
        build_resp = await client.post("/api/v1/builds", json={})
        assert build_resp.status_code == 201
        build_id = build_resp.json()["id"]

        task_resp = await client.post(
            f"/api/v1/builds/{build_id}/tasks",
            json={
                "task_id": task_id,
                "task_namespace": "",
                "task_name": task_id,
                "task_data": {},
                "dependency_task_ids": [],
            },
        )
        assert task_resp.status_code == 201
        return build_id, task_resp.json()["id"]

    async def test_register_task_sets_pending(
        self, client: AsyncClient, async_session: AsyncSession
    ) -> None:
        _, task_pk = await self._create_build_and_task(client, "tc-pending")
        row = await async_session.get(Task, UUID(task_pk))
        assert row is not None
        assert row.latest_status == TaskStatus.PENDING
        assert row.latest_status_event_id is not None

    async def test_start_then_complete(
        self, client: AsyncClient, async_session: AsyncSession
    ) -> None:
        build_id, task_pk = await self._create_build_and_task(client, "tc-flow")
        await client.post(f"/api/v1/builds/{build_id}/tasks/tc-flow/start")
        async_session.expire_all()
        row = await async_session.get(Task, UUID(task_pk))
        assert row is not None and row.latest_status == TaskStatus.RUNNING
        assert row.latest_started_at is not None

        await client.post(f"/api/v1/builds/{build_id}/tasks/tc-flow/complete")
        async_session.expire_all()
        row = await async_session.get(Task, UUID(task_pk))
        assert row is not None and row.latest_status == TaskStatus.COMPLETED
        assert row.latest_completed_at is not None

    async def test_fail_records_error(
        self, client: AsyncClient, async_session: AsyncSession
    ) -> None:
        build_id, task_pk = await self._create_build_and_task(client, "tc-fail")
        await client.post(f"/api/v1/builds/{build_id}/tasks/tc-fail/start")
        await client.post(
            f"/api/v1/builds/{build_id}/tasks/tc-fail/fail",
            params={"error_message": "kaboom"},
        )
        async_session.expire_all()
        row = await async_session.get(Task, UUID(task_pk))
        assert row is not None
        assert row.latest_status == TaskStatus.FAILED
        assert row.latest_error_message == "kaboom"


# ---------------------------------------------------------------------------
# get_all_task_global_statuses now reads denormalised columns
# ---------------------------------------------------------------------------


class TestReadsFromColumns:
    async def test_unknown_tasks_default_to_pending(
        self, async_session: AsyncSession
    ) -> None:
        random_id = generate_uuid7()
        result = await get_all_task_global_statuses(async_session, [random_id])
        status, *_, waiting, commit = result[random_id]
        assert status == TaskStatus.PENDING
        assert waiting is False
        assert commit is None

    async def test_reads_columns_not_events(self, async_session: AsyncSession) -> None:
        # Insert a task with explicitly-set latest_* columns and NO events.
        # Old impl would have returned PENDING (no events). New impl reads
        # the columns and returns COMPLETED.
        task = Task(
            id=generate_uuid7(),
            task_id="reads-from-cols",
            environment_id=DEFAULT_ENVIRONMENT_ID,
            task_namespace="",
            task_name="t",
            task_data={},
            is_phantom=False,
            latest_status=TaskStatus.COMPLETED,
            latest_status_at=datetime.now(timezone.utc),
            latest_completed_at=datetime.now(timezone.utc),
            latest_waiting_for_lock=False,
            latest_commit_hash="deadbeef",
        )
        async_session.add(task)
        await async_session.commit()

        result = await get_all_task_global_statuses(async_session, [task.id])
        (
            status,
            _started_at,
            _completed_at,
            _err,
            _build_id,
            waiting,
            commit,
        ) = result[task.id]
        assert status == TaskStatus.COMPLETED
        assert waiting is False
        assert commit == "deadbeef"


# ---------------------------------------------------------------------------
# Lock-with-completion path also updates latest_status
# ---------------------------------------------------------------------------


class TestLockReleaseCompletion:
    async def test_release_with_completion_marks_task_completed(
        self, client: AsyncClient, async_session: AsyncSession
    ) -> None:
        from uuid import uuid4

        build_resp = await client.post("/api/v1/builds", json={})
        build_id = build_resp.json()["id"]
        task_resp = await client.post(
            f"/api/v1/builds/{build_id}/tasks",
            json={
                "task_id": "lock-task",
                "task_namespace": "",
                "task_name": "lock-task",
                "task_data": {},
                "dependency_task_ids": [],
            },
        )
        task_pk = task_resp.json()["id"]
        await client.post(f"/api/v1/builds/{build_id}/tasks/lock-task/start")

        owner_id = str(uuid4())
        await client.post(
            "/api/v1/locks/lock-task/acquire",
            json={
                "owner_id": owner_id,
                "ttl_seconds": 60,
                "check_task_completion": False,
            },
        )
        await client.post(
            "/api/v1/locks/lock-task/release",
            json={
                "owner_id": owner_id,
                "task_completed": True,
                "build_id": build_id,
            },
        )
        async_session.expire_all()
        row = await async_session.get(Task, UUID(task_pk))
        assert row is not None and row.latest_status == TaskStatus.COMPLETED
