"""Plan-only builds (``api-pg`` tier).

A plan-only build registers its roots and their static closure under its
scope and runs nothing. It is created PENDING, never RUNNING, and ends
REGISTERED through ``POST /builds/{id}/registered``. What makes it safe is
that every read handing out or waking work acts on RUNNING builds only, so
these tests check the consumers rather than the flag: the frontier, the
claiming start, wake-up flagging, ``wake-candidates`` and
``stalled-builds`` -- and the routes that could otherwise turn it into an
ordinary build (``resume``, the reactive marker, ``complete``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import RowMapping, text

from stardag_api.schemas_v2 import RegistrationItem
from stardag_api.services import builds, exclusion, reactive, wakeups
from stardag_api.services.errors import Conflict
from stardag_api.services.transitions import Transition
from tests.v2_support import ENV, Harness, item, observed, unexpanded


@pytest.fixture
def h(session_factory) -> Harness:
    return Harness(session_factory)


async def _svc(h: Harness, fn: Any, *args: Any, **kwargs: Any) -> Any:
    async with h.sf() as s:
        return await fn(s, ENV, *args, **kwargs)


async def _sql(h: Harness, sql: str, **params: Any) -> None:
    async with h.sf() as s:
        await s.execute(text(sql), params)
        await s.commit()


async def _plan_only(
    h: Harness,
    roots: list[RegistrationItem],
    members: list[RegistrationItem],
    *,
    deployment_id: UUID | None = None,
    seal: bool = True,
) -> tuple[UUID, UUID]:
    """A plan-only build with its plan registered (and sealed); returns
    (build, plan)."""
    build = await _svc(
        h,
        builds.create_build,
        build_id=uuid4(),
        root_task_ids=[r.task_id for r in roots],
        plan_only=True,
    )
    deployment_id = deployment_id or await h.new_deployment()
    plan = await h.plan(build.id, deployment_id, roots)
    await h.register(plan.id, members)
    if seal:
        await h.seal(plan.id)
    return build.id, plan.id


async def _build_events(h: Harness, build_id: UUID) -> list[RowMapping]:
    return [
        e for e in await h.events(build_id=build_id) if e["type"].startswith("build_")
    ]


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------


async def test_a_plan_only_build_is_created_pending_and_never_started(h: Harness):
    root = item("Root")
    build_id = uuid4()
    build = await _svc(
        h,
        builds.create_build,
        build_id=build_id,
        root_task_ids=[root.task_id],
        plan_only=True,
    )
    assert (build.status, build.plan_only, build.started_at) == (
        "pending",
        True,
        None,
    )
    (started,) = await _build_events(h, build_id)
    assert started["type"] == "build_started"
    assert started["event_metadata"] == {"plan_only": True}

    again = await _svc(
        h,
        builds.create_build,
        build_id=build_id,
        root_task_ids=[root.task_id],
        plan_only=True,
    )
    assert again.id == build_id and again.status == "pending"
    with pytest.raises(Conflict) as exc:
        await _svc(
            h, builds.create_build, build_id=build_id, root_task_ids=[root.task_id]
        )
    assert exc.value.code == "build_id_conflict"


# --------------------------------------------------------------------------
# Register, seal, end REGISTERED -- nothing is ever handed out
# --------------------------------------------------------------------------


async def test_registered_none_started(h: Harness):
    """The whole static closure lands (every member expanded, complete or
    not), the frontier hands out nothing, a claiming start is refused, and
    ``registered`` ends the build REGISTERED without any member COMPLETED."""
    leaf = item("Leaf", observed_complete=True)
    mid = item("Mid", upstreams=[leaf])
    root = item("Root", upstreams=[mid])
    deployment = await h.new_deployment()
    build, plan = await _plan_only(
        h, [root], [leaf, mid, root], deployment_id=deployment
    )

    members = await h.members(plan)
    assert set(members) == {leaf.task_id, mid.task_id, root.task_id}
    for it in (leaf, mid, root):
        instance = await h.instance(deployment, it)
        assert instance is not None and instance["expanded_at"] is not None
    assert (await h.task(leaf))["status"] == "completed"

    front = await h.frontier(build)
    assert front.sealed
    assert (front.runnable, front.discovery_jobs, front.running) == ([], [], [])
    with pytest.raises(Conflict) as exc:
        await h.start(plan, mid)
    assert exc.value.code == "build_not_running"

    done = await _svc(h, builds.register_build, build)
    assert (done.status, done.plan_only) == ("registered", True)
    assert done.completed_at is not None
    again = await _svc(h, builds.register_build, build)
    assert again.completed_at == done.completed_at
    types = [e["type"] for e in await _build_events(h, build)]
    assert types == ["build_started", "build_registered"]
    (registered,) = await h.events(build_id=build, types=["build_registered"])
    assert registered["event_metadata"] == {"plan_id": str(plan)}
    assert registered["plan_id"] is None  # build-level: CHECK on ``event``

    assert await h.count("execution") == 0
    assert (await h.task(mid))["status"] == "pending"
    assert (await h.frontier(build)).runnable == []


async def test_registered_needs_a_sealed_plan_and_a_plan_only_build(h: Harness):
    root = item("Root")
    build, plan = await _plan_only(h, [root], [root], seal=False)
    with pytest.raises(Conflict) as exc:
        await _svc(h, builds.register_build, build)
    assert (exc.value.code, exc.value.detail["reason"]) == (
        "plan_incomplete",
        "not_sealed",
    )
    await h.seal(plan)
    assert (await _svc(h, builds.register_build, build)).status == "registered"

    ordinary, _ = await h.planned([root], [root], seal=True)
    with pytest.raises(Conflict) as exc:
        await _svc(h, builds.register_build, ordinary)
    assert exc.value.code == "build_not_plan_only"


async def test_registered_with_no_plan_is_not_sealed(h: Harness):
    root = item("Root")
    build = await _svc(
        h, builds.create_build, root_task_ids=[root.task_id], plan_only=True
    )
    with pytest.raises(Conflict) as exc:
        await _svc(h, builds.register_build, build.id)
    assert exc.value.detail == {"reason": "not_sealed", "plan_id": None}


# --------------------------------------------------------------------------
# What a plan-only build refuses, and what it allows
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["complete", "exit_early", "resume", "reactive"])
async def test_a_plan_only_build_cannot_be_made_to_run(h: Harness, action: str):
    """``complete``, ``exit-early``, ``resume`` and the reactive marker are
    409 ``build_plan_only``, before and after it is REGISTERED (after,
    ``complete`` and ``exit-early`` meet the sticky terminal status first)."""
    root = item("Root")
    build, _ = await _plan_only(h, [root], [root])
    calls = {
        "complete": lambda: _svc(h, builds.complete_build, build, force=True),
        "exit_early": lambda: _svc(h, builds.exit_early, build),
        "resume": lambda: _svc(h, builds.resume_build, build),
        "reactive": lambda: _svc(
            h, reactive.set_reactive_meta, build, app_name="app", tick_kwargs=None
        ),
    }
    with pytest.raises(Conflict) as exc:
        await calls[action]()
    assert exc.value.code == "build_plan_only"
    row = await h.build(build)
    assert (row["status"], row["reactive_app_name"]) == ("pending", None)

    await _svc(h, builds.register_build, build)
    with pytest.raises(Conflict) as exc:
        await calls[action]()
    expected = (
        "build_terminal" if action in ("complete", "exit_early") else "build_plan_only"
    )
    assert exc.value.code == expected
    assert (await h.build(build))["status"] == "registered"


@pytest.mark.parametrize("terminal", ["fail", "cancel"])
async def test_a_registration_gone_wrong_can_be_failed_or_cancelled(
    h: Harness, terminal: str
):
    """A plan-only build left PENDING (its driver died or gave up) can be
    closed; once closed, ``registered`` meets the sticky terminal status."""
    root = item("Root")
    build, _ = await _plan_only(h, [root], [root], seal=False)
    fn = builds.fail_build if terminal == "fail" else builds.cancel_build
    closed = await _svc(h, fn, build)
    assert closed.status == ("failed" if terminal == "fail" else "cancelled")
    with pytest.raises(Conflict) as exc:
        await _svc(h, builds.register_build, build)
    assert exc.value.code == "build_terminal"


@pytest.mark.parametrize("closed", ["pending", "failed", "registered"])
async def test_skip_blocked_is_a_no_op_on_a_plan_only_build(h: Harness, closed: str):
    """A plan-only build has no failure to propagate, and SKIPPED is a
    global task status: a member blocked by an upstream another build
    failed is left as it is, whatever state the plan-only build is in."""
    deployment = await h.new_deployment()
    leaf = item("Leaf")
    root = item("Root", upstreams=[leaf])
    plan_only, _ = await _plan_only(h, [root], [leaf, root], deployment_id=deployment)
    _, other = await h.planned([leaf], [leaf], deployment_id=deployment, seal=True)
    execution = await h.start(other.id, leaf)
    await h.transition(other.id, leaf, Transition.fail(execution, "boom"))
    if closed == "failed":
        await _svc(h, builds.fail_build, plan_only, error_message="gave up")
    elif closed == "registered":
        await _svc(h, builds.register_build, plan_only)

    result = await _svc(h, exclusion.skip_blocked, plan_only)
    assert (result.plan_id, result.skipped) == (None, [])
    assert (await h.task(root))["status"] == "pending"


# --------------------------------------------------------------------------
# Wake-ups: never flagged, never a candidate, never stalled
# --------------------------------------------------------------------------


@pytest.mark.parametrize("registered", [False, True])
async def test_a_plan_only_build_is_never_woken_or_reported_stalled(
    h: Harness, registered: bool
):
    """A status change on a task a plan-only build shares with a RUNNING
    reactive build flags only the reactive one. The plan-only build is
    given the reactive marker and an old flag behind the API's back, so the
    status alone is what keeps it out of ``wake-candidates`` and
    ``stalled-builds``."""
    deployment = await h.new_deployment()
    t = item("T")
    plan_only, _ = await _plan_only(h, [t], [t], deployment_id=deployment)
    if registered:
        await _svc(h, builds.register_build, plan_only)
    other, other_plan = await h.planned([t], [t], deployment_id=deployment, seal=True)
    await _svc(h, reactive.set_reactive_meta, other, app_name="app", tick_kwargs=None)
    await _sql(
        h, "UPDATE build SET reactive_app_name = 'app' WHERE id = :b", b=plan_only
    )
    await _sql(h, "UPDATE build_wake SET needs_tick_at = NULL")

    await h.start(other_plan.id, t)  # a status change of T, through ``other``
    assert (await h.build(plan_only))["needs_tick_at"] is None

    await _sql(
        h,
        "UPDATE build_wake SET needs_tick_at = now() - interval '10 minutes'",
    )
    candidates = await _svc(h, wakeups.wake_candidates)
    assert plan_only not in {c.build_id for c in candidates}
    stalled = await _svc(h, wakeups.stalled_builds, older_than=timedelta(minutes=5))
    assert plan_only not in {s.build_id for s in stalled}
    notified = await _svc(h, wakeups.notify, plan_only, can_spawn=True)
    assert not notified.needs_tick


async def test_registering_observations_still_wake_other_builds(h: Harness):
    """A plan-only build's registration is an ordinary observation: a
    target found complete moves the task COMPLETED, and the RUNNING
    reactive builds holding it are flagged, as by any other build."""
    deployment = await h.new_deployment()
    t = item("T")
    other, _ = await h.planned([t], [t], deployment_id=deployment, seal=True)
    await _svc(h, reactive.set_reactive_meta, other, app_name="app", tick_kwargs=None)
    await _sql(h, "UPDATE build_wake SET needs_tick_at = NULL")

    await _plan_only(h, [t], [observed(t, True)], deployment_id=deployment)
    assert (await h.task(t))["status"] == "completed"
    assert (await h.build(other))["needs_tick_at"] is not None


# --------------------------------------------------------------------------
# Over HTTP
# --------------------------------------------------------------------------


async def test_plan_only_over_http(client: AsyncClient, h: Harness):
    deployment = await h.new_deployment()
    root = item("Root")
    created = await client.post(
        "/api/v2/builds", json={"root_task_ids": [root.task_id], "plan_only": True}
    )
    assert created.status_code == 200, created.text
    build = created.json()
    assert (build["status"], build["plan_only"]) == ("pending", True)
    plan = (
        await client.post(
            f"/api/v2/builds/{build['id']}/plans",
            json={
                "plan_id": str(uuid4()),
                "deployment_id": str(deployment),
                "roots": [unexpanded(root).model_dump(mode="json")],
            },
        )
    ).json()
    await client.post(
        f"/api/v2/plans/{plan['id']}/members",
        json={"items": [root.model_dump(mode="json")]},
    )
    early = await client.post(f"/api/v2/builds/{build['id']}/registered")
    assert early.status_code == 409
    assert early.json()["detail"]["code"] == "plan_incomplete"
    assert (await client.post(f"/api/v2/plans/{plan['id']}/seal")).status_code == 200

    resumed = await client.post(f"/api/v2/builds/{build['id']}/resume", json={})
    assert resumed.status_code == 409
    assert resumed.json()["detail"]["code"] == "build_plan_only"

    done = await client.post(f"/api/v2/builds/{build['id']}/registered")
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "registered"
    listed = (
        await client.get("/api/v2/builds", params={"status": "registered"})
    ).json()
    assert [b["id"] for b in listed["builds"]] == [build["id"]]


async def test_an_ordinary_build_reads_plan_only_false(h: Harness):
    build, _ = await h.planned([item("Root")])
    row = await h.build(build)
    assert row["plan_only"] is False and row["status"] == "running"
