"""Terminating abandoned builds and releasing the claims they hold.

Covers the cascade on single-build cancel, the bulk-cancel/reaper endpoint,
and — most importantly — that the reaper's idleness signal reflects real
activity rather than ``Build.last_active_at``, which task events never touch.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import Build, Event

BULK_CANCEL = "/api/v1/builds/bulk-cancel"


def _register(task_id: str, deps: list[str] | None = None) -> dict:
    return {
        "task_id": task_id,
        "task_namespace": "",
        "task_name": "T",
        "task_data": {},
        "dependency_task_ids": deps or [],
    }


async def _new_build(client: AsyncClient, **body) -> str:
    return (await client.post("/api/v1/builds", json=body)).json()["id"]


async def _start(
    client: AsyncClient,
    build_id: str,
    task_id: str,
    limit_keys: list[str] | None = None,
) -> None:
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=_register(task_id))
    params: dict = {"executor": "modal", "executor_ref": f"fc-{task_id}"}
    if limit_keys:
        params["limit_key"] = limit_keys
    await client.post(f"/api/v1/builds/{build_id}/tasks/{task_id}/start", params=params)


async def _backdate(
    session: AsyncSession,
    build_id: str,
    *,
    age: timedelta,
    events: bool = True,
) -> datetime:
    """Age a build by ``age``: its ``last_active_at`` and, optionally, every
    event it owns. Returns the timestamp used.

    Event timestamps are rewritten one microsecond apart in their original
    order rather than all set to the same instant — build status is derived
    from *which build-level event came last*, so collapsing them would
    change the build's status, not just its age.
    """
    when = datetime.now(timezone.utc) - age
    pk = UUID(build_id)
    await session.execute(
        update(Build).where(Build.id == pk).values(last_active_at=when)
    )
    if events:
        rows = (
            (
                await session.execute(
                    select(Event)
                    .where(Event.build_id == pk)
                    .order_by(Event.created_at.asc(), Event.id.asc())
                )
            )
            .scalars()
            .all()
        )
        for offset, event in enumerate(rows):
            event.created_at = when + timedelta(microseconds=offset)
    await session.commit()
    return when


async def _status(client: AsyncClient, build_id: str) -> str:
    return (await client.get(f"/api/v1/builds/{build_id}")).json()["status"]


async def _task_status(client: AsyncClient, task_id: str) -> str:
    return (await client.get(f"/api/v1/tasks/{task_id}")).json()["latest_status"]


# ---------------------------------------------------------------------------
# cascade= on single-build cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_without_cascade_leaves_the_claim_held(client: AsyncClient):
    """The historical behaviour, kept as the default: a build-level event
    only. The task stays RUNNING and keeps denying its claim."""
    build_id = await _new_build(client)
    await _start(client, build_id, "keep-running", ["gpu"])

    response = await client.post(f"/api/v1/builds/{build_id}/cancel")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["cascaded_task_ids"] == []
    assert body["cascaded_task_count"] == 0

    assert await _task_status(client, "keep-running") == "running"
    holders = (await client.get("/api/v1/concurrency-limits/gpu/holders")).json()
    assert [h["task_id"] for h in holders["holders"]] == ["keep-running"]


@pytest.mark.asyncio
async def test_cancel_cascade_releases_claims_and_limit_slots(client: AsyncClient):
    """cascade=true is the fix: the task leaves RUNNING, so the claim is
    free for the next build and the concurrency-limit slot is free too."""
    await client.put("/api/v1/concurrency-limits/gpu", json={"max_concurrent": 1})
    build_id = await _new_build(client)
    await _start(client, build_id, "held", ["gpu"])
    await _start(client, build_id, "suspended-held")
    await client.post(f"/api/v1/builds/{build_id}/tasks/suspended-held/suspend")
    await client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register("still-pending")
    )

    response = await client.post(
        f"/api/v1/builds/{build_id}/cancel", params={"cascade": "true"}
    )
    assert response.status_code == 200
    body = response.json()
    assert sorted(body["cascaded_task_ids"]) == ["held", "suspended-held"]
    assert body["cascaded_task_count"] == 2

    assert await _task_status(client, "held") == "cancelled"
    assert await _task_status(client, "suspended-held") == "cancelled"
    # PENDING is deliberately untouched — it holds no claim, and cancelling
    # it would reach into builds that merely reference the same task.
    assert await _task_status(client, "still-pending") == "pending"

    # The slot is genuinely free: a new build can acquire it under the cap
    # of 1, which would be denied if the claim had leaked.
    other = await _new_build(client)
    await client.post(f"/api/v1/builds/{other}/tasks", json=_register("next-up"))
    response = await client.post(
        f"/api/v1/builds/{other}/tasks/next-up/start",
        params={"limit_key": ["gpu"], "enforce_limits": "true", "claim": "true"},
    )
    assert response.status_code == 200
    holders = (await client.get("/api/v1/concurrency-limits/gpu/holders")).json()
    assert [h["task_id"] for h in holders["holders"]] == ["next-up"]


@pytest.mark.asyncio
async def test_cascade_never_cancels_another_builds_running_task(
    client: AsyncClient,
):
    """A task this build merely referenced, running under another build, is
    that build's claim to release — the server cannot stop a live worker, so
    declaring somebody else's task dead is the worst possible outcome."""
    owner = await _new_build(client)
    await _start(client, owner, "shared")

    referencer = await _new_build(client)
    await client.post(f"/api/v1/builds/{referencer}/tasks", json=_register("shared"))

    response = await client.post(
        f"/api/v1/builds/{referencer}/cancel", params={"cascade": "true"}
    )
    assert response.json()["cascaded_task_ids"] == []
    assert await _task_status(client, "shared") == "running"

    # Cancelling the owner does release it.
    response = await client.post(
        f"/api/v1/builds/{owner}/cancel", params={"cascade": "true"}
    )
    assert response.json()["cascaded_task_ids"] == ["shared"]
    assert await _task_status(client, "shared") == "cancelled"


@pytest.mark.asyncio
async def test_cancel_cascade_environment_isolation(
    client: AsyncClient, as_environment_b
):
    build_id = await _new_build(client)
    await _start(client, build_id, "iso-claim")

    with as_environment_b():
        response = await client.post(
            f"/api/v1/builds/{build_id}/cancel", params={"cascade": "true"}
        )
        assert response.status_code == 403

    assert await _status(client, build_id) == "running"
    assert await _task_status(client, "iso-claim") == "running"


# ---------------------------------------------------------------------------
# Bulk cancel by explicit ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_cancel_requires_a_filter(client: AsyncClient):
    """ "Cancel everything running here" is not a cleanup operation."""
    response = await client.post(BULK_CANCEL, json={})
    assert response.status_code == 422
    assert "idle_for_seconds" in str(response.json()["detail"])


@pytest.mark.asyncio
async def test_bulk_cancel_by_ids_cascades_by_default(client: AsyncClient):
    a, b = await _new_build(client), await _new_build(client)
    await _start(client, a, "bulk-a")
    await _start(client, b, "bulk-b")
    untouched = await _new_build(client)

    response = await client.post(BULK_CANCEL, json={"build_ids": [a, b]})
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is False
    assert body["build_count"] == 2
    assert body["task_count"] == 2
    assert {r["build_id"] for r in body["builds"]} == {a, b}
    assert body["truncated"] is False

    assert await _status(client, a) == "cancelled"
    assert await _status(client, b) == "cancelled"
    assert await _status(client, untouched) == "running"
    assert await _task_status(client, "bulk-a") == "cancelled"


@pytest.mark.asyncio
async def test_bulk_cancel_cascade_can_be_turned_off(client: AsyncClient):
    build_id = await _new_build(client)
    await _start(client, build_id, "no-cascade")

    body = (
        await client.post(BULK_CANCEL, json={"build_ids": [build_id], "cascade": False})
    ).json()
    assert body["task_count"] == 0
    assert await _status(client, build_id) == "cancelled"
    assert await _task_status(client, "no-cascade") == "running"


@pytest.mark.asyncio
async def test_bulk_cancel_is_idempotent_and_reports_why_ids_were_skipped(
    client: AsyncClient,
):
    running = await _new_build(client)
    done = await _new_build(client)
    await client.post(f"/api/v1/builds/{done}/complete")
    reactive = await _new_build(client)
    await client.put(
        f"/api/v1/builds/{reactive}/reactive-meta", json={"app_name": "app"}
    )
    unknown = "00000000-0000-0000-0000-0000000000ff"

    body = (
        await client.post(
            BULK_CANCEL, json={"build_ids": [running, done, reactive, unknown]}
        )
    ).json()
    assert [r["build_id"] for r in body["builds"]] == [running]
    assert body["skipped"] == {
        done: "not_running",
        reactive: "reactive",
        unknown: "not_found",
    }

    # Re-running is a no-op: the build is terminal now, so it is skipped
    # rather than cancelled twice.
    again = (await client.post(BULK_CANCEL, json={"build_ids": [running]})).json()
    assert again["build_count"] == 0
    assert again["skipped"] == {running: "not_running"}


@pytest.mark.asyncio
async def test_bulk_cancel_cannot_reach_another_environment(
    client: AsyncClient, as_environment_b
):
    """Another environment's ids are reported as not_found — identical to an
    unknown id, so the endpoint can't be used to probe for build ids."""
    build_id = await _new_build(client)
    await _start(client, build_id, "iso-bulk")

    with as_environment_b():
        body = (await client.post(BULK_CANCEL, json={"build_ids": [build_id]})).json()
        assert body["build_count"] == 0
        assert body["skipped"] == {build_id: "not_found"}

    assert await _status(client, build_id) == "running"
    assert await _task_status(client, "iso-bulk") == "running"


@pytest.mark.asyncio
async def test_bulk_cancel_is_admin_gated_on_the_user_auth_path(
    client: AsyncClient, role_auth_switcher
):
    """Destructive and workspace-wide, so the JWT path needs ADMIN; API keys
    (environment-scoped machine credentials) stay unrestricted."""
    build_id = await _new_build(client)

    with role_auth_switcher["member"]():
        response = await client.post(BULK_CANCEL, json={"build_ids": [build_id]})
        assert response.status_code == 403
        assert "admin" in response.json()["detail"].lower()
    assert await _status(client, build_id) == "running"

    with role_auth_switcher["api_key"]():
        response = await client.post(BULK_CANCEL, json={"build_ids": [build_id]})
        assert response.status_code == 200
    assert await _status(client, build_id) == "cancelled"


@pytest.mark.asyncio
async def test_bulk_cancel_requires_auth(unauthenticated_client: AsyncClient):
    response = await unauthenticated_client.post(BULK_CANCEL, json={"build_ids": []})
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_bulk_cancel_limit_truncates_and_reports_it(
    client: AsyncClient, async_session
):
    builds = [await _new_build(client) for _ in range(3)]
    for build_id in builds:
        await _backdate(async_session, build_id, age=timedelta(days=2))

    body = (
        await client.post(BULK_CANCEL, json={"idle_for_seconds": 3600, "limit": 2})
    ).json()
    assert body["build_count"] == 2
    assert body["truncated"] is True

    # A second call drains the rest.
    body = (
        await client.post(BULK_CANCEL, json={"idle_for_seconds": 3600, "limit": 2})
    ).json()
    assert body["build_count"] == 1
    assert body["truncated"] is False


# ---------------------------------------------------------------------------
# The reaper: idleness must reflect activity, not Build.last_active_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_with_recent_task_activity_is_not_reaped(
    client: AsyncClient, async_session
):
    """**The** safety property of the reaper.

    ``Build.last_active_at`` is bumped by build-level lifecycle events only —
    task events deliberately never touch it, so the per-task hot path doesn't
    contend on the build row. A build that has been busily running tasks for
    days therefore still shows its BUILD_STARTED timestamp there. If the
    reaper measured idleness with that column it would cancel live work.

    Here: a build whose lifecycle timestamp AND build-level events are two
    days old, but which registered and started a task seconds ago. It must
    survive.
    """
    busy = await _new_build(client)
    await _backdate(async_session, busy, age=timedelta(days=2))
    # Recent task activity, after the backdating.
    await _start(client, busy, "busy-task")
    # The column the naive implementation would have used is still ancient.
    build_row = (
        await async_session.execute(select(Build).where(Build.id == UUID(busy)))
    ).scalar_one()
    assert datetime.now(timezone.utc) - build_row.last_active_at.replace(
        tzinfo=timezone.utc
    ) > timedelta(days=1)

    # A genuinely abandoned build, for contrast.
    abandoned = await _new_build(client)
    await _start(client, abandoned, "abandoned-task")
    await _backdate(async_session, abandoned, age=timedelta(days=2))

    body = (await client.post(BULK_CANCEL, json={"idle_for_seconds": 24 * 3600})).json()
    assert [r["build_id"] for r in body["builds"]] == [abandoned]

    assert await _status(client, busy) == "running"
    assert await _task_status(client, "busy-task") == "running"
    assert await _status(client, abandoned) == "cancelled"
    assert await _task_status(client, "abandoned-task") == "cancelled"


@pytest.mark.asyncio
async def test_reaper_respects_a_pending_scheduler_wakeup(
    client: AsyncClient, async_session
):
    """needs_tick_at means a worker reported progress and the scheduler
    hasn't run yet — the opposite of abandoned."""
    build_id = await _new_build(client)
    await _backdate(async_session, build_id, age=timedelta(days=2))
    await client.post(f"/api/v1/builds/{build_id}/notify")

    body = (await client.post(BULK_CANCEL, json={"idle_for_seconds": 3600})).json()
    assert body["build_count"] == 0
    assert await _status(client, build_id) == "running"

    # Once the tick consumes the wake-up, the build is idle again.
    await client.delete(f"/api/v1/builds/{build_id}/notify")
    body = (await client.post(BULK_CANCEL, json={"idle_for_seconds": 3600})).json()
    assert [r["build_id"] for r in body["builds"]] == [build_id]


@pytest.mark.asyncio
async def test_reaper_only_touches_running_builds(client: AsyncClient, async_session):
    """Terminal builds are never re-cancelled — including a build that was
    completed and then RESUMED, whose latest build-level event makes it
    RUNNING again despite an earlier terminal one."""
    completed = await _new_build(client)
    await client.post(f"/api/v1/builds/{completed}/complete")
    failed = await _new_build(client)
    await client.post(f"/api/v1/builds/{failed}/fail")

    resumed = await _new_build(client)
    await _start(client, resumed, "resumed-task")
    await client.post(f"/api/v1/builds/{resumed}/complete")
    await client.post(f"/api/v1/builds/{resumed}/resume")
    assert await _status(client, resumed) == "running"

    for build_id in (completed, failed, resumed):
        await _backdate(async_session, build_id, age=timedelta(days=2))

    body = (await client.post(BULK_CANCEL, json={"idle_for_seconds": 3600})).json()
    assert [r["build_id"] for r in body["builds"]] == [resumed]
    assert await _status(client, completed) == "completed"
    assert await _status(client, failed) == "failed"


@pytest.mark.asyncio
async def test_reaper_excludes_reactive_builds_unless_asked(
    client: AsyncClient, async_session
):
    """A reactive build is quiet between ticks by design and has its own
    watchdog; silence is not evidence of abandonment."""
    reactive = await _new_build(client)
    await client.put(
        f"/api/v1/builds/{reactive}/reactive-meta", json={"app_name": "my-app"}
    )
    plain = await _new_build(client)
    for build_id in (reactive, plain):
        await _backdate(async_session, build_id, age=timedelta(days=2))

    body = (await client.post(BULK_CANCEL, json={"idle_for_seconds": 3600})).json()
    assert [r["build_id"] for r in body["builds"]] == [plain]
    assert await _status(client, reactive) == "running"

    body = (
        await client.post(
            BULK_CANCEL,
            json={"idle_for_seconds": 3600, "include_reactive": True},
        )
    ).json()
    assert [r["build_id"] for r in body["builds"]] == [reactive]
    assert body["builds"][0]["reactive_app_name"] == "my-app"


@pytest.mark.asyncio
async def test_reaper_can_scope_to_one_reactive_app(client: AsyncClient, async_session):
    """Naming an app implies including reactive builds — and only that
    app's, so retiring one deployment can't sweep another's."""
    mine, theirs = await _new_build(client), await _new_build(client)
    await client.put(f"/api/v1/builds/{mine}/reactive-meta", json={"app_name": "gone"})
    await client.put(
        f"/api/v1/builds/{theirs}/reactive-meta", json={"app_name": "live"}
    )
    for build_id in (mine, theirs):
        await _backdate(async_session, build_id, age=timedelta(days=2))

    body = (
        await client.post(
            BULK_CANCEL, json={"idle_for_seconds": 3600, "reactive_app_name": "gone"}
        )
    ).json()
    assert [r["build_id"] for r in body["builds"]] == [mine]
    assert await _status(client, theirs) == "running"


@pytest.mark.asyncio
async def test_reaper_threshold_is_a_lower_bound(client: AsyncClient, async_session):
    """A build idle for less than the threshold is left alone."""
    build_id = await _new_build(client)
    await _backdate(async_session, build_id, age=timedelta(minutes=30))

    body = (await client.post(BULK_CANCEL, json={"idle_for_seconds": 24 * 3600})).json()
    assert body["build_count"] == 0

    body = (await client.post(BULK_CANCEL, json={"idle_for_seconds": 600})).json()
    assert [r["build_id"] for r in body["builds"]] == [build_id]


@pytest.mark.asyncio
async def test_reaper_threshold_has_a_floor(client: AsyncClient):
    """A threshold small enough to race a live build is a foot-gun."""
    response = await client.post(BULK_CANCEL, json={"idle_for_seconds": 5})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_dry_run_changes_nothing_but_reports_everything(
    client: AsyncClient, async_session
):
    build_id = await _new_build(client)
    await _start(client, build_id, "dry-claim")
    await _backdate(async_session, build_id, age=timedelta(days=2))

    before_events = (
        (
            await async_session.execute(
                select(Event).where(Event.build_id == UUID(build_id))
            )
        )
        .scalars()
        .all()
    )

    body = (
        await client.post(BULK_CANCEL, json={"idle_for_seconds": 3600, "dry_run": True})
    ).json()
    assert body["dry_run"] is True
    assert body["build_count"] == 1
    assert body["task_count"] == 1
    assert body["builds"][0]["build_id"] == build_id
    assert body["builds"][0]["cascaded_task_ids"] == ["dry-claim"]
    assert body["builds"][0]["last_activity_at"] is not None

    # Nothing written.
    assert await _status(client, build_id) == "running"
    assert await _task_status(client, "dry-claim") == "running"
    after_events = (
        (
            await async_session.execute(
                select(Event).where(Event.build_id == UUID(build_id))
            )
        )
        .scalars()
        .all()
    )
    assert len(after_events) == len(before_events)

    # And the real run does exactly what the dry run promised.
    real = (await client.post(BULK_CANCEL, json={"idle_for_seconds": 3600})).json()
    assert real["builds"][0]["cascaded_task_ids"] == ["dry-claim"]


@pytest.mark.asyncio
async def test_reaper_records_reason_and_source_on_the_event(
    client: AsyncClient, async_session
):
    build_id = await _new_build(client)
    await _backdate(async_session, build_id, age=timedelta(days=2))

    await client.post(
        BULK_CANCEL,
        json={"idle_for_seconds": 3600, "reason": "orchestrator host retired"},
    )
    events = (await client.get(f"/api/v1/builds/{build_id}/events")).json()
    cancel_event = next(e for e in events if e["event_type"] == "build_cancelled")
    assert cancel_event["error_message"] == "orchestrator host retired"
    assert cancel_event["event_metadata"]["cancelled_by"] == "bulk_cancel"
    assert cancel_event["event_metadata"]["reason"] == "orchestrator host retired"


# ---------------------------------------------------------------------------
# The idleness signal on build responses (so the UI can show it)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_response_exposes_both_liveness_timestamps(
    client: AsyncClient, async_session
):
    """`last_active_at` is the ordering column; `last_activity_at` is what
    the reaper measures. They differ exactly when task activity happened
    after the last lifecycle event — which is most of a build's life."""
    build_id = await _new_build(client)
    await _backdate(async_session, build_id, age=timedelta(days=2))
    await _start(client, build_id, "activity-task")

    for body in (
        (await client.get(f"/api/v1/builds/{build_id}")).json(),
        (await client.get("/api/v1/builds")).json()["builds"][0],
    ):
        assert body["last_active_at"] is not None
        assert body["last_activity_at"] is not None
        assert body["last_activity_at"] > body["last_active_at"]


# ---------------------------------------------------------------------------
# The optional unattended sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_reaper_is_off_by_default():
    """It cancels other people's work and every replica runs its own timer,
    so it must never start unless explicitly enabled."""
    from stardag_api.config import ReaperSettings

    assert ReaperSettings().enabled is False


@pytest.mark.asyncio
async def test_sweep_stale_builds_service_matches_the_endpoint(
    client: AsyncClient, async_session
):
    """The in-process sweep is the same code path as the endpoint, minus
    auth — and spans every environment (no environment_id)."""
    from stardag_api.services.build_cleanup import sweep_stale_builds

    build_id = await _new_build(client)
    await _start(client, build_id, "sweep-claim")
    await _backdate(async_session, build_id, age=timedelta(days=2))

    cancelled = await sweep_stale_builds(
        async_session, idle_before=datetime.now(timezone.utc) - timedelta(hours=1)
    )
    assert [str(c.build.id) for c in cancelled] == [build_id]
    assert cancelled[0].cascaded_task_ids == ["sweep-claim"]
    assert await _status(client, build_id) == "cancelled"

    # Idempotent: the second sweep finds nothing.
    assert (
        await sweep_stale_builds(
            async_session, idle_before=datetime.now(timezone.utc) - timedelta(hours=1)
        )
        == []
    )


# ---------------------------------------------------------------------------
# Postgres: the RUNNING predicate and the idle filter are non-trivial SQL
# (correlated aggregates, NULL handling) and SQLite is forgiving.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_selection_postgres(pg_client, pg_session):
    busy = await _new_build(pg_client)
    await _backdate(pg_session, busy, age=timedelta(days=2))
    await _start(pg_client, busy, "pg-busy")

    abandoned = await _new_build(pg_client)
    await _start(pg_client, abandoned, "pg-abandoned")
    await _backdate(pg_session, abandoned, age=timedelta(days=2))

    done = await _new_build(pg_client)
    await pg_client.post(f"/api/v1/builds/{done}/complete")
    await _backdate(pg_session, done, age=timedelta(days=2))

    body = (await pg_client.post(BULK_CANCEL, json={"idle_for_seconds": 3600})).json()
    assert [r["build_id"] for r in body["builds"]] == [abandoned]
    assert await _status(pg_client, busy) == "running"
    assert await _status(pg_client, done) == "completed"
    assert await _task_status(pg_client, "pg-abandoned") == "cancelled"


@pytest.mark.asyncio
async def test_list_tasks_status_filter_postgres(pg_client):
    """The claim-enumeration query, on the dialect the index is built for."""
    build_id = await _new_build(pg_client)
    await _start(pg_client, build_id, "pg-running")
    await pg_client.post(
        f"/api/v1/builds/{build_id}/tasks", json=_register("pg-pending")
    )

    listed = (await pg_client.get("/api/v1/tasks", params={"status": "running"})).json()
    assert [t["task_id"] for t in listed["tasks"]] == ["pg-running"]
    assert listed["tasks"][0]["latest_status_build_id"] == build_id

    stale = (
        await pg_client.get(
            "/api/v1/tasks",
            params={
                "status": "running",
                "status_older_than": (
                    datetime.now(timezone.utc) - timedelta(days=1)
                ).isoformat(),
            },
        )
    ).json()
    assert stale["tasks"] == []


# ---------------------------------------------------------------------------
# GET /builds?idle_for_seconds= — the same idleness predicate, as a filter.
#
# The UI's "idle for" control previews what the reaper would cancel, so it
# must use the reaper's definition and must not be answered over a bounded
# window: dropping the oldest matches is exactly the failure this filter
# exists to avoid.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_builds_idle_filter_total_is_exact_and_paginates_server_side(
    client: AsyncClient, async_session
):
    """`total` is a COUNT(*) over a real predicate, not "matches in the
    window", and the pages partition the matches exactly."""
    idle = [await _new_build(client) for _ in range(5)]
    for build_id in idle:
        await _backdate(async_session, build_id, age=timedelta(days=2))
    fresh = [await _new_build(client) for _ in range(3)]

    first = (
        await client.get(
            "/api/v1/builds", params={"idle_for_seconds": 3600, "page_size": 2}
        )
    ).json()
    assert first["total"] == 5
    assert len(first["builds"]) == 2

    seen: list[str] = []
    for page in (1, 2, 3):
        body = (
            await client.get(
                "/api/v1/builds",
                params={"idle_for_seconds": 3600, "page_size": 2, "page": page},
            )
        ).json()
        assert body["total"] == 5
        seen.extend(b["id"] for b in body["builds"])
    assert len(seen) == len(set(seen)) == 5
    assert set(seen) == set(idle)
    assert not set(seen) & set(fresh)

    # Unfiltered still sees everything.
    assert (await client.get("/api/v1/builds")).json()["total"] == 8


@pytest.mark.asyncio
async def test_list_builds_idle_filter_orders_stalest_first(
    client: AsyncClient, async_session
):
    """Stalest first, so a capped page holds what an operator wants to act
    on — the reverse of the default most-recently-active-first order."""
    ages: dict[int, str] = {}
    for days in (2, 5, 9):
        build_id = await _new_build(client)
        await _backdate(async_session, build_id, age=timedelta(days=days))
        ages[days] = build_id

    body = (
        await client.get("/api/v1/builds", params={"idle_for_seconds": 3600})
    ).json()
    assert [b["id"] for b in body["builds"]] == [ages[9], ages[5], ages[2]]

    # Without the filter, the default ordering is unchanged.
    body = (await client.get("/api/v1/builds")).json()
    assert [b["id"] for b in body["builds"]] == [ages[2], ages[5], ages[9]]


@pytest.mark.asyncio
async def test_list_builds_idle_filter_ignores_builds_with_recent_task_activity(
    client: AsyncClient, async_session
):
    """Same signal as the reaper, so the same guard: a build whose
    ``last_active_at`` is ancient but which ran a task seconds ago is not
    idle. If this filter and the reaper disagreed, the UI would be offering
    operators a preview of something that will not happen."""
    busy = await _new_build(client)
    await _backdate(async_session, busy, age=timedelta(days=2))
    await _start(client, busy, "list-busy-task")

    abandoned = await _new_build(client)
    await _start(client, abandoned, "list-abandoned-task")
    await _backdate(async_session, abandoned, age=timedelta(days=2))

    body = (
        await client.get("/api/v1/builds", params={"idle_for_seconds": 24 * 3600})
    ).json()
    assert [b["id"] for b in body["builds"]] == [abandoned]
    assert body["total"] == 1

    # And the reaper agrees, which is the point of sharing the predicate.
    preview = (
        await client.post(
            BULK_CANCEL, json={"idle_for_seconds": 24 * 3600, "dry_run": True}
        )
    ).json()
    assert [b["build_id"] for b in preview["builds"]] == [abandoned]


@pytest.mark.asyncio
async def test_list_builds_idle_filter_respects_pending_scheduler_wakeup(
    client: AsyncClient, async_session
):
    build_id = await _new_build(client)
    await _backdate(async_session, build_id, age=timedelta(days=2))
    await client.post(f"/api/v1/builds/{build_id}/notify")

    body = (
        await client.get("/api/v1/builds", params={"idle_for_seconds": 3600})
    ).json()
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_list_builds_idle_filter_has_the_same_floor_as_bulk_cancel(
    client: AsyncClient,
):
    """One definition of a legal threshold — the two must not disagree."""
    assert (
        await client.get("/api/v1/builds", params={"idle_for_seconds": 5})
    ).status_code == 422
    assert (
        await client.post(BULK_CANCEL, json={"idle_for_seconds": 5})
    ).status_code == 422
    assert (
        await client.get("/api/v1/builds", params={"idle_for_seconds": 60})
    ).status_code == 200


@pytest.mark.asyncio
async def test_list_builds_idle_filter_environment_isolation(
    client: AsyncClient, as_environment_b
):
    build_id = await _new_build(client)

    with as_environment_b():
        body = (
            await client.get("/api/v1/builds", params={"idle_for_seconds": 60})
        ).json()
        assert body == {"builds": [], "total": 0, "page": 1, "page_size": 20}

    assert build_id  # owning environment sees its own builds unchanged
    assert (await client.get("/api/v1/builds")).json()["total"] == 1


@pytest.mark.asyncio
async def test_list_builds_running_and_idle_is_exact_beyond_the_scan_cap(
    client: AsyncClient, async_session, monkeypatch
):
    """`status=running` alone derives status in Python over the 500
    most-recently-active candidates. Combined with `idle_for_seconds` that
    cap would drop precisely the oldest builds — the ones the query exists
    to find — so the pair uses the SQL RUNNING predicate instead.

    The cap is monkeypatched down (as the existing truncation test does)
    rather than building 500 rows.
    """
    from stardag_api.routes import builds as builds_routes

    monkeypatch.setattr(builds_routes, "_STATUS_FILTER_SCAN_CAP", 2)

    # Three old RUNNING builds and one old terminal one. With a cap of 2,
    # a window scan could see at most two of them.
    old_running = []
    for days in (10, 9, 8):
        build_id = await _new_build(client)
        await _backdate(async_session, build_id, age=timedelta(days=days))
        old_running.append(build_id)
    old_done = await _new_build(client)
    await client.post(f"/api/v1/builds/{old_done}/complete")
    await _backdate(async_session, old_done, age=timedelta(days=7))

    # Two fresh RUNNING builds, which is what the most-recently-active
    # window would be full of.
    for _ in range(2):
        await _new_build(client)

    body = (
        await client.get(
            "/api/v1/builds",
            params={"status": "running", "idle_for_seconds": 24 * 3600},
        )
    ).json()
    assert body["total"] == 3
    assert [b["id"] for b in body["builds"]] == old_running  # stalest first
    assert all(b["status"] == "running" for b in body["builds"])

    # The bare status filter is unchanged — still window-scanned, still
    # truncating at the (patched) cap.
    capped = (await client.get("/api/v1/builds", params={"status": "running"})).json()
    assert capped["total"] <= 2


@pytest.mark.asyncio
async def test_list_builds_idle_filter_implies_running(
    client: AsyncClient, async_session
):
    """A finished build is not idle, however long ago it finished.

    Without this the filter degrades into "builds nothing has happened to
    lately", which every completed build satisfies permanently — so the
    listing fills with history exactly as the operator is trying to find
    the handful of builds that are stuck. It also has to hold for the
    listing to preview `POST /builds/bulk-cancel`, which only ever cancels
    running builds.
    """
    abandoned = await _new_build(client)
    await _backdate(async_session, abandoned, age=timedelta(days=2))

    for endpoint in ("complete", "fail"):
        finished = await _new_build(client)
        await client.post(f"/api/v1/builds/{finished}/{endpoint}")
        # Older than the abandoned one, so a pure-idleness query would sort
        # it *first* rather than merely include it.
        await _backdate(async_session, finished, age=timedelta(days=30))

    body = (
        await client.get("/api/v1/builds", params={"idle_for_seconds": 3600})
    ).json()
    assert [b["id"] for b in body["builds"]] == [abandoned]
    assert body["total"] == 1

    # All three are still listed when idleness is not what is being asked.
    assert (await client.get("/api/v1/builds")).json()["total"] == 3

    # And the reaper's dry run picks the same single build — the parity
    # that makes this listing a usable preview.
    preview = (
        await client.post(BULK_CANCEL, json={"idle_for_seconds": 3600, "dry_run": True})
    ).json()
    assert [b["build_id"] for b in preview["builds"]] == [abandoned]


@pytest.mark.asyncio
async def test_list_builds_rejects_other_statuses_with_idle_filter(
    client: AsyncClient,
):
    """A contradiction, not a narrower query: idle already means running."""
    response = await client.get(
        "/api/v1/builds", params={"status": "completed", "idle_for_seconds": 3600}
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "idle_for_seconds" in detail and "completed" in detail

    # Each alone is fine.
    assert (
        await client.get("/api/v1/builds", params={"status": "completed"})
    ).status_code == 200
    assert (
        await client.get("/api/v1/builds", params={"idle_for_seconds": 3600})
    ).status_code == 200


@pytest.mark.asyncio
async def test_list_builds_idle_filter_postgres(pg_client, pg_session):
    """Correlated aggregates + COUNT(*) over them, on the real dialect."""
    busy = await _new_build(pg_client)
    await _backdate(pg_session, busy, age=timedelta(days=2))
    await _start(pg_client, busy, "pg-list-busy")

    abandoned = await _new_build(pg_client)
    await _backdate(pg_session, abandoned, age=timedelta(days=2))

    done = await _new_build(pg_client)
    await pg_client.post(f"/api/v1/builds/{done}/complete")
    await _backdate(pg_session, done, age=timedelta(days=3))

    body = (
        await pg_client.get("/api/v1/builds", params={"idle_for_seconds": 3600})
    ).json()
    # `done` is older than `abandoned` and has had no activity for longer,
    # but it finished — it is not idle, it is over.
    assert body["total"] == 1
    assert [b["id"] for b in body["builds"]] == [abandoned]

    # Asking for running as well is redundant, and answers the same.
    running_only = (
        await pg_client.get(
            "/api/v1/builds",
            params={"status": "running", "idle_for_seconds": 3600},
        )
    ).json()
    assert running_only["total"] == 1
    assert [b["id"] for b in running_only["builds"]] == [abandoned]
