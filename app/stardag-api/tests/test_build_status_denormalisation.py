"""Tests for the denormalised ``Build.latest_*`` columns.

Build status used to be recomputed from the build's event stream on every
read, which meant ``GET /builds?status=`` could not filter in SQL and the
stale-build reaper needed a second, independent SQL encoding of the same
rule. These tests pin down that there is now one rule, on the row:

- ``apply_event_to_build`` encodes what the historical event replay did.
- Every build lifecycle endpoint folds its event into the columns — miss one
  and the row silently drifts from the event log.
- The columns are what reads, filters, pagination and the reaper all consult,
  so they cannot disagree.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import Build, BuildStatus, Event, EventType
from stardag_api.models.base import generate_uuid7
from stardag_api.services.status import apply_event_to_build
from tests.conftest import DEFAULT_ENVIRONMENT_ID

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _new_build() -> Build:
    return Build(
        id=generate_uuid7(),
        environment_id=DEFAULT_ENVIRONMENT_ID,
        name="b1",
        root_task_ids=[],
        latest_status=BuildStatus.PENDING,
        latest_is_resumed=False,
    )


def _event(
    et: EventType,
    *,
    offset_seconds: int = 0,
    metadata: dict | None = None,
) -> Event:
    return Event(
        id=generate_uuid7(),
        build_id=generate_uuid7(),
        task_id=None,
        event_type=et,
        created_at=T0 + timedelta(seconds=offset_seconds),
        event_metadata=metadata,
    )


async def _db_build(session: AsyncSession, build_id: str) -> Build:
    session.expire_all()
    row = await session.get(Build, UUID(build_id))
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# apply_event_to_build — the rule itself
# ---------------------------------------------------------------------------


class TestApplyEventToBuild:
    def test_started_asserts_running_with_started_at(self) -> None:
        build = _new_build()
        apply_event_to_build(build, _event(EventType.BUILD_STARTED))
        assert build.latest_status == BuildStatus.RUNNING
        assert build.latest_started_at == T0
        assert build.latest_completed_at is None
        assert build.latest_is_resumed is False

    @pytest.mark.parametrize(
        ("event_type", "status"),
        [
            (EventType.BUILD_COMPLETED, BuildStatus.COMPLETED),
            (EventType.BUILD_FAILED, BuildStatus.FAILED),
            (EventType.BUILD_CANCELLED, BuildStatus.CANCELLED),
            (EventType.BUILD_EXIT_EARLY, BuildStatus.EXIT_EARLY),
        ],
    )
    def test_terminals_set_status_and_completed_at(
        self, event_type: EventType, status: BuildStatus
    ) -> None:
        build = _new_build()
        apply_event_to_build(build, _event(EventType.BUILD_STARTED))
        apply_event_to_build(build, _event(event_type, offset_seconds=10))
        assert build.latest_status == status
        assert build.latest_completed_at == T0 + timedelta(seconds=10)
        assert build.latest_started_at == T0  # untouched by the terminal
        assert build.latest_is_resumed is False

    @pytest.mark.parametrize(
        "event_type",
        [
            EventType.BUILD_COMPLETED,
            EventType.BUILD_FAILED,
            EventType.BUILD_CANCELLED,
        ],
    )
    def test_terminals_record_the_triggering_user(self, event_type: EventType) -> None:
        build = _new_build()
        apply_event_to_build(
            build, _event(event_type, metadata={"triggered_by_user_id": "u-7"})
        )
        assert build.latest_status_triggered_by_user_id == "u-7"

    def test_exit_early_is_never_user_triggered(self) -> None:
        """The SDK emits it ("everything left is running elsewhere"), so it
        carries no attribution even if metadata sneaks one in."""
        build = _new_build()
        apply_event_to_build(
            build,
            _event(
                EventType.BUILD_EXIT_EARLY,
                metadata={"triggered_by_user_id": "u-7"},
            ),
        )
        assert build.latest_status_triggered_by_user_id is None

    def test_resume_reasserts_running_and_clears_completed_at(self) -> None:
        build = _new_build()
        apply_event_to_build(build, _event(EventType.BUILD_STARTED))
        apply_event_to_build(
            build,
            _event(
                EventType.BUILD_FAILED,
                offset_seconds=10,
                metadata={"triggered_by_user_id": "u-9"},
            ),
        )
        apply_event_to_build(build, _event(EventType.BUILD_RESUMED, offset_seconds=20))

        assert build.latest_status == BuildStatus.RUNNING
        assert build.latest_is_resumed is True
        # Cleared, so no stale "completed at" survives the resume...
        assert build.latest_completed_at is None
        assert build.latest_status_triggered_by_user_id is None
        # ...but the build still started when it first started.
        assert build.latest_started_at == T0

    def test_a_terminal_after_a_resume_clears_the_resumed_flag(self) -> None:
        build = _new_build()
        apply_event_to_build(build, _event(EventType.BUILD_RESUMED))
        apply_event_to_build(
            build, _event(EventType.BUILD_COMPLETED, offset_seconds=10)
        )
        assert build.latest_status == BuildStatus.COMPLETED
        assert build.latest_is_resumed is False

    def test_a_start_after_a_resume_clears_the_resumed_flag(self) -> None:
        """The SDK never emits this sequence, but the fold must not depend on
        that — ``is_resumed`` means "the event that produced the current
        status was a resume", full stop."""
        build = _new_build()
        apply_event_to_build(build, _event(EventType.BUILD_RESUMED))
        apply_event_to_build(build, _event(EventType.BUILD_STARTED, offset_seconds=10))
        assert build.latest_status == BuildStatus.RUNNING
        assert build.latest_is_resumed is False

    def test_no_stickiness_a_completed_build_can_resume(self) -> None:
        """Unlike a task, where COMPLETED means "the target exists" and is
        sticky, a build genuinely goes COMPLETED → RUNNING:
        ``sd.build(resume_build_id=...)`` on a finished build is supported."""
        build = _new_build()
        apply_event_to_build(build, _event(EventType.BUILD_COMPLETED))
        apply_event_to_build(build, _event(EventType.BUILD_RESUMED, offset_seconds=10))
        assert build.latest_status == BuildStatus.RUNNING

    def test_task_events_are_no_ops(self) -> None:
        build = _new_build()
        apply_event_to_build(build, _event(EventType.BUILD_STARTED))
        for et in (
            EventType.TASK_STARTED,
            EventType.TASK_FAILED,
            EventType.TASK_COMPLETED,
        ):
            apply_event_to_build(build, _event(et, offset_seconds=10))
        assert build.latest_status == BuildStatus.RUNNING
        assert build.latest_completed_at is None

    def test_ties_resolve_in_arrival_order_not_timestamp_order(self) -> None:
        """Two events sharing a ``created_at`` are still applied one after the
        other, and the later-applied one wins.

        This is the tie-break decision. The event replay this fold replaces
        ordered on ``created_at`` and let the index settle ties; the reaper's
        separate SQL predicate read a tie as *not* running. Folding at write
        time removes the question: the commits are strictly ordered even when
        the timestamps collide, so "last" means what the server actually saw
        last.
        """
        cancelled_last = _new_build()
        apply_event_to_build(cancelled_last, _event(EventType.BUILD_RESUMED))
        apply_event_to_build(cancelled_last, _event(EventType.BUILD_CANCELLED))
        assert cancelled_last.latest_status == BuildStatus.CANCELLED

        resumed_last = _new_build()
        apply_event_to_build(resumed_last, _event(EventType.BUILD_CANCELLED))
        apply_event_to_build(resumed_last, _event(EventType.BUILD_RESUMED))
        assert resumed_last.latest_status == BuildStatus.RUNNING
        assert resumed_last.latest_is_resumed is True


# ---------------------------------------------------------------------------
# Every lifecycle endpoint folds its event into the row.
#
# These assert on the ``builds`` row itself, not on the response body: a
# handler that forgot the fold but still recomputed the response some other
# way would pass an endpoint test and leave the row — which the list filter
# and the reaper both trust — permanently wrong.
# ---------------------------------------------------------------------------


class TestLifecycleEndpointsUpdateTheRow:
    async def test_create_build_starts_running(
        self, client: AsyncClient, async_session: AsyncSession
    ) -> None:
        build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
        row = await _db_build(async_session, build_id)
        assert row.latest_status == BuildStatus.RUNNING
        assert row.latest_started_at is not None
        assert row.latest_completed_at is None
        assert row.latest_is_resumed is False

    @pytest.mark.parametrize(
        ("path", "status"),
        [
            ("complete", BuildStatus.COMPLETED),
            ("fail", BuildStatus.FAILED),
            ("cancel", BuildStatus.CANCELLED),
            ("exit-early", BuildStatus.EXIT_EARLY),
        ],
    )
    async def test_terminal_endpoints(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
        path: str,
        status: BuildStatus,
    ) -> None:
        build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
        response = await client.post(f"/api/v1/builds/{build_id}/{path}")
        assert response.status_code == 200

        row = await _db_build(async_session, build_id)
        assert row.latest_status == status
        assert row.latest_completed_at is not None
        assert row.latest_started_at is not None  # the start is not lost
        # The row and the response cannot disagree — they are the same data.
        assert response.json()["status"] == status.value

    async def test_terminal_endpoints_record_triggered_by_user(
        self, client: AsyncClient, async_session: AsyncSession
    ) -> None:
        build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
        await client.post(
            f"/api/v1/builds/{build_id}/cancel",
            params={"triggered_by_user_id": "default-local-user"},
        )
        row = await _db_build(async_session, build_id)
        assert row.latest_status_triggered_by_user_id == "default-local-user"

        body = (await client.get(f"/api/v1/builds/{build_id}")).json()
        assert body["status_triggered_by_user"]["id"] == "default-local-user"

    async def test_cascading_cancel_folds_the_build_too(
        self, client: AsyncClient, async_session: AsyncSession
    ) -> None:
        """The cascade takes task row locks; the build must still be folded,
        and must be locked first so the two orders can't deadlock."""
        build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
        await client.post(
            f"/api/v1/builds/{build_id}/tasks",
            json={
                "task_id": "casc-t",
                "task_namespace": "",
                "task_name": "T",
                "task_data": {},
                "dependency_task_ids": [],
            },
        )
        await client.post(f"/api/v1/builds/{build_id}/tasks/casc-t/start")

        response = await client.post(
            f"/api/v1/builds/{build_id}/cancel", params={"cascade": "true"}
        )
        assert response.json()["cascaded_task_ids"] == ["casc-t"]
        row = await _db_build(async_session, build_id)
        assert row.latest_status == BuildStatus.CANCELLED

    async def test_bulk_cancel_folds_every_build_it_cancels(
        self, client: AsyncClient, async_session: AsyncSession
    ) -> None:
        build_ids = [
            (await client.post("/api/v1/builds", json={})).json()["id"]
            for _ in range(3)
        ]
        response = await client.post(
            "/api/v1/builds/bulk-cancel", json={"build_ids": build_ids}
        )
        assert response.json()["build_count"] == 3
        for build_id in build_ids:
            row = await _db_build(async_session, build_id)
            assert row.latest_status == BuildStatus.CANCELLED
            assert row.latest_completed_at is not None

    async def test_resume_preserves_started_at_and_clears_completed_at(
        self, client: AsyncClient, async_session: AsyncSession
    ) -> None:
        build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
        started_at = (await _db_build(async_session, build_id)).latest_started_at

        await client.post(f"/api/v1/builds/{build_id}/fail")
        failed = await _db_build(async_session, build_id)
        assert failed.latest_completed_at is not None

        body = (await client.post(f"/api/v1/builds/{build_id}/resume")).json()
        assert body["status"] == "running"
        assert body["is_resumed"] is True
        assert body["completed_at"] is None

        row = await _db_build(async_session, build_id)
        assert row.latest_status == BuildStatus.RUNNING
        assert row.latest_is_resumed is True
        assert row.latest_completed_at is None
        assert row.latest_started_at == started_at

    async def test_no_op_resume_of_a_fresh_build_leaves_the_row_alone(
        self, client: AsyncClient, async_session: AsyncSession
    ) -> None:
        """No BUILD_RESUMED event is recorded for a build with no activity
        beyond its start, so nothing is folded either."""
        build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
        body = (await client.post(f"/api/v1/builds/{build_id}/resume")).json()
        assert body["is_resumed"] is False

        row = await _db_build(async_session, build_id)
        assert row.latest_status == BuildStatus.RUNNING
        assert row.latest_is_resumed is False

    async def test_the_row_matches_a_replay_of_the_whole_lifecycle(
        self, client: AsyncClient, async_session: AsyncSession
    ) -> None:
        """End to end: drive a build through every transition, then fold its
        recorded events from scratch and check the two agree. The row is the
        event stream, not an approximation of it."""
        build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
        await client.post(f"/api/v1/builds/{build_id}/fail")
        await client.post(
            f"/api/v1/builds/{build_id}/tasks",
            json={
                "task_id": "replay-t",
                "task_namespace": "",
                "task_name": "T",
                "task_data": {},
                "dependency_task_ids": [],
            },
        )
        await client.post(f"/api/v1/builds/{build_id}/resume")
        await client.post(
            f"/api/v1/builds/{build_id}/complete",
            params={"triggered_by_user_id": "default-local-user"},
        )

        events = (
            (
                await async_session.execute(
                    select(Event)
                    .where(Event.build_id == UUID(build_id))
                    .where(Event.task_id.is_(None))
                    .order_by(Event.created_at.asc(), Event.id.asc())
                )
            )
            .scalars()
            .all()
        )
        replayed = _new_build()
        for event in events:
            apply_event_to_build(replayed, event)

        row = await _db_build(async_session, build_id)
        assert row.latest_status == replayed.latest_status == BuildStatus.COMPLETED
        assert row.latest_started_at == replayed.latest_started_at
        assert row.latest_completed_at == replayed.latest_completed_at
        assert row.latest_is_resumed == replayed.latest_is_resumed is False
        assert (
            row.latest_status_triggered_by_user_id
            == replayed.latest_status_triggered_by_user_id
            == "default-local-user"
        )


# ---------------------------------------------------------------------------
# GET /builds?status= — exact and unbounded, for every status.
# ---------------------------------------------------------------------------


class TestStatusFilterIsExact:
    async def test_total_is_a_true_count_and_pages_partition_it(
        self, client: AsyncClient
    ) -> None:
        """Not "matches within the scanned window". The old implementation
        scanned the 500 most-recently-active candidates, filtered in Python
        and paginated the survivors in memory."""
        failed = []
        for _ in range(5):
            build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
            await client.post(f"/api/v1/builds/{build_id}/fail")
            failed.append(build_id)
        for _ in range(3):
            await client.post("/api/v1/builds", json={})

        seen: list[str] = []
        for page in (1, 2, 3):
            body = (
                await client.get(
                    "/api/v1/builds",
                    params={"status": "failed", "page_size": 2, "page": page},
                )
            ).json()
            assert body["total"] == 5
            seen.extend(b["id"] for b in body["builds"])
        assert len(seen) == len(set(seen)) == 5
        assert set(seen) == set(failed)

        # ...and the page really was cut server-side.
        first = (
            await client.get(
                "/api/v1/builds", params={"status": "failed", "page_size": 2}
            )
        ).json()
        assert len(first["builds"]) == 2

    @pytest.mark.parametrize(
        ("path", "status"),
        [
            ("complete", "completed"),
            ("fail", "failed"),
            ("cancel", "cancelled"),
            ("exit-early", "exit_early"),
        ],
    )
    async def test_every_status_filters_in_sql(
        self, client: AsyncClient, path: str, status: str
    ) -> None:
        match = (await client.post("/api/v1/builds", json={})).json()["id"]
        await client.post(f"/api/v1/builds/{match}/{path}")
        other = (await client.post("/api/v1/builds", json={})).json()["id"]

        body = (await client.get("/api/v1/builds", params={"status": status})).json()
        assert body["total"] == 1
        assert [b["id"] for b in body["builds"]] == [match]

        running = (
            await client.get("/api/v1/builds", params={"status": "running"})
        ).json()
        assert [b["id"] for b in running["builds"]] == [other]

    async def test_pending_builds_are_filterable(
        self, client: AsyncClient, async_session: AsyncSession
    ) -> None:
        """A build row with no build-level events at all — reachable only by
        direct insertion, but it is a status the enum has and the filter must
        answer for."""
        build = _new_build()
        async_session.add(build)
        await async_session.commit()
        await client.post("/api/v1/builds", json={})

        body = (await client.get("/api/v1/builds", params={"status": "pending"})).json()
        assert body["total"] == 1
        assert [b["id"] for b in body["builds"]] == [str(build.id)]


# ---------------------------------------------------------------------------
# The reaper reads the same column.
# ---------------------------------------------------------------------------


class TestReaperSelectsFromTheColumn:
    async def _stale(self, client: AsyncClient, session: AsyncSession) -> str:
        build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
        when = datetime.now(timezone.utc) - timedelta(days=2)
        row = await _db_build(session, build_id)
        row.last_active_at = when
        for event in (
            (
                await session.execute(
                    select(Event).where(Event.build_id == UUID(build_id))
                )
            )
            .scalars()
            .all()
        ):
            event.created_at = when
        await session.commit()
        return build_id

    async def test_only_running_builds_are_selected(
        self, client: AsyncClient, async_session: AsyncSession
    ) -> None:
        running = await self._stale(client, async_session)
        done = await self._stale(client, async_session)
        await client.post(f"/api/v1/builds/{done}/complete")

        preview = (
            await client.post(
                "/api/v1/builds/bulk-cancel",
                json={"idle_for_seconds": 3600, "dry_run": True},
            )
        ).json()
        assert [b["build_id"] for b in preview["builds"]] == [running]

        # And the list endpoint, filtering on the same column, agrees.
        listed = (
            await client.get(
                "/api/v1/builds",
                params={"status": "running", "idle_for_seconds": 3600},
            )
        ).json()
        assert [b["id"] for b in listed["builds"]] == [running]

    async def test_a_resume_that_arrived_last_makes_a_build_reapable(
        self, client: AsyncClient, async_session: AsyncSession
    ) -> None:
        """The tie-break, where it costs money.

        A terminal event and a resume sharing a ``created_at`` used to be read
        as "not running" by the reaper's own SQL predicate, deliberately, and
        arbitrarily by the status replay — the two disagreed. Now both read
        the column, which records the order the events actually arrived: the
        resume landed last, so the build is running, and a build that is
        running and has since been silent for the idle window is exactly what
        the reaper is for.
        """
        build_id = await self._stale(client, async_session)
        await client.post(f"/api/v1/builds/{build_id}/cancel")
        await client.post(f"/api/v1/builds/{build_id}/resume")

        # Collapse every event onto one instant: the timestamps now tie, and
        # the row still says what arrived last.
        when = datetime.now(timezone.utc) - timedelta(days=2)
        row = await _db_build(async_session, build_id)
        row.last_active_at = when
        for event in (
            (
                await async_session.execute(
                    select(Event).where(Event.build_id == UUID(build_id))
                )
            )
            .scalars()
            .all()
        ):
            event.created_at = when
        await async_session.commit()

        assert (await _db_build(async_session, build_id)).latest_status == (
            BuildStatus.RUNNING
        )
        preview = (
            await client.post(
                "/api/v1/builds/bulk-cancel",
                json={"idle_for_seconds": 3600, "dry_run": True},
            )
        ).json()
        assert [b["build_id"] for b in preview["builds"]] == [build_id]
