"""Wake-ups, notify, wake-candidates, the scheduler lease, reactive meta
and tick summaries (``api-pg`` tier).

Written from design.md, "Wake-ups, limits, locks": "builds holding a task"
is ``plan_member`` of active plans, not "any event in the build"; flagging
on every transition and on limit-slot release; the lease owner-checked on
``build`` columns. v1's semantics otherwise carry over
(``research/v1-server-logic.md`` §5).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from stardag_api.config import settings as app_settings
from stardag_api.services import builds, reactive, wakeups
from stardag_api.services.errors import Conflict
from stardag_api.services.transitions import Transition
from tests.v2_support import ENV, Harness, item, observed


@pytest.fixture
def h(session_factory) -> Harness:
    return Harness(session_factory)


async def _reactive(h: Harness, *build_ids: UUID, app: str = "app") -> None:
    for build_id in build_ids:
        async with h.sf() as s:
            await reactive.set_reactive_meta(
                s, ENV, build_id, app_name=app, tick_kwargs=None
            )


async def _flagged(h: Harness, build_id: UUID) -> bool:
    return (await h.build(build_id))["needs_tick_at"] is not None


async def _clear(h: Harness, *build_ids: UUID) -> None:
    for build_id in build_ids:
        async with h.sf() as s:
            await wakeups.clear_notify(s, ENV, build_id)


async def _sql(h: Harness, sql: str, **params: Any) -> None:
    async with h.sf() as s:
        await s.execute(text(sql), params)
        await s.commit()


async def _svc(h: Harness, fn: Any, *args: Any, **kwargs: Any) -> Any:
    async with h.sf() as s:
        return await fn(s, ENV, *args, **kwargs)


# --------------------------------------------------------------------------
# Flagging over membership
# --------------------------------------------------------------------------


async def test_flagging_is_over_active_plan_membership_not_events(h: Harness):
    """Only RUNNING reactive builds whose **active** plan holds the task (a
    non-excluded member) are flagged: not a build whose only membership is
    a superseded plan (v1 flagged it — it had events for the task), not a
    build that excluded the member, not a non-reactive or terminal build,
    and not the build whose own transition it was."""
    deployment = await h.new_deployment()
    t = item("T")
    root = item("Root", upstreams=[t])
    writer, plan_w = await h.planned([t], [t], deployment_id=deployment)
    holder, _ = await h.planned([t], [t], deployment_id=deployment)
    passive, _ = await h.planned([t], [t], deployment_id=deployment)
    finished, _ = await h.planned([t], [t], deployment_id=deployment)
    excluding, plan_x = await h.planned([t], [t], deployment_id=deployment)

    moved_on = await h.new_build([root])
    old = await h.plan(moved_on, deployment, [root])
    await h.register(old.id, [t, root])
    await h.seal(old.id)
    new = await h.plan(moved_on, deployment, [root], settings={"S": "2"})
    await h.register(
        new.id, [observed(root.model_copy(update={"declared_upstreams": None}), True)]
    )
    await h.seal(new.id)  # T is only in the superseded plan now

    await _reactive(h, writer, holder, finished, excluding, moved_on)
    await _svc(h, builds.cancel_build, finished)
    await _sql(
        h,
        "UPDATE plan_member SET excluded_at = now(), excluded_reason = 'operator'"
        " WHERE plan_id = :p",
        p=plan_x.id,
    )
    await _clear(h, writer, holder, passive, finished, excluding, moved_on)

    await h.start(plan_w.id, t)
    assert await _flagged(h, holder)
    for build in (writer, passive, finished, excluding, moved_on):
        assert not await _flagged(h, build), build


async def test_every_status_change_flags_and_a_no_op_does_not(h: Harness):
    """Into RUNNING, out of it, an observation from registration, a retry:
    every change of ``task.status`` flags; a transition that changes nothing
    (a renewal, an observation of an already-COMPLETED task) does not."""
    deployment = await h.new_deployment()
    t = item("T")
    _, plan_a = await h.planned([t], [t], deployment_id=deployment)
    build_b, _ = await h.planned([t], [t], deployment_id=deployment)
    await _reactive(h, build_b)

    async def changed(action: Any) -> bool:
        await _clear(h, build_b)
        await action()
        return await _flagged(h, build_b)

    execution = uuid4()
    assert await changed(lambda: h.start(plan_a.id, t, execution))
    assert not await changed(lambda: h.renew(t, execution))
    assert await changed(
        lambda: h.transition(plan_a.id, t, Transition.fail(execution, "x"))
    )
    assert await changed(lambda: h.transition(plan_a.id, t, Transition.retry()))
    assert await changed(lambda: h.register(plan_a.id, [observed(t, True)]))
    assert not await changed(lambda: h.register(plan_a.id, [observed(t, True)]))


async def test_a_build_release_flags_the_neighbours_it_unblocks(h: Harness):
    """A cancel releases the build's claims; each release is a transition
    of its own, so the other builds holding the task are flagged."""
    deployment = await h.new_deployment()
    t = item("T")
    build_a, plan_a = await h.planned([t], [t], deployment_id=deployment)
    build_b, _ = await h.planned([t], [t], deployment_id=deployment)
    await _reactive(h, build_a, build_b)
    await h.start(plan_a.id, t)
    await _clear(h, build_b)
    await _svc(h, builds.cancel_build, build_a)
    assert await _flagged(h, build_b)


async def test_limit_slot_release_flags_the_builds_queued_on_the_key(h: Harness):
    """A claim beyond a key's limit is refused (409
    ``concurrency_limit_reached``) and records the keys it asked for; when a
    holder of the key leaves RUNNING, the builds with an actionable member
    queued on it are flagged although they do not hold the holder's task —
    and the queued task can then claim."""
    await _sql(
        h,
        "INSERT INTO environment_concurrency_limit (id, environment_id, key,"
        " max_concurrent) VALUES (:id, :env, 'gpu', 1)",
        id=uuid4(),
        env=ENV,
    )
    t1, t2 = item("T1"), item("T2")
    _, plan_a = await h.planned([t1], [t1])
    build_b, plan_b = await h.planned([t2], [t2])
    await _reactive(h, build_b)
    holder = await h.start(plan_a.id, t1, limit_keys=["gpu"])

    with pytest.raises(Conflict) as exc:
        await h.start(plan_b.id, t2, limit_keys=["gpu"])
    assert exc.value.code == "concurrency_limit_reached"
    assert exc.value.detail["keys"] == ["gpu"]
    assert (await h.task(t2))["status"] == "pending"
    await _clear(h, build_b)

    await h.transition(plan_a.id, t1, Transition.complete(holder))
    assert await _flagged(h, build_b)
    await h.start(plan_b.id, t2, limit_keys=["gpu"])


async def test_a_lapsed_claim_holds_no_slot(h: Harness):
    await _sql(
        h,
        "INSERT INTO environment_concurrency_limit (id, environment_id, key,"
        " max_concurrent) VALUES (:id, :env, 'db', 1)",
        id=uuid4(),
        env=ENV,
    )
    t1, t2 = item("T1"), item("T2")
    _, plan_a = await h.planned([t1], [t1])
    _, plan_b = await h.planned([t2], [t2])
    await h.start(plan_a.id, t1, limit_keys=["db"])
    await h.lapse_claim(t1)
    await h.start(plan_b.id, t2, limit_keys=["db"])


# --------------------------------------------------------------------------
# notify, wake-candidates
# --------------------------------------------------------------------------


async def test_notify_flags_a_running_build_and_reports_a_live_scheduler(
    h: Harness,
):
    """POST sets the flag on a RUNNING build and stamps the hand-out mark
    (the caller will spawn); with a live lease it reports
    ``scheduler_live`` and puts the stamp back. A terminal build is not
    flagged. GET reads the flag; DELETE clears it."""
    t = item("T")
    build, _ = await h.planned([t], [t])
    state = await _svc(h, wakeups.notify, build)
    assert state.needs_tick and state.scheduler_live is False
    row = await h.build(build)
    assert row["needs_tick_at"] is not None and row["tick_requested_at"] is not None

    await _svc(h, wakeups.clear_notify, build)
    await _sql(h, "UPDATE build SET tick_requested_at = NULL WHERE id = :b", b=build)
    await _svc(h, wakeups.acquire_lease, build, owner_id="tick-1", ttl_seconds=60)
    state = await _svc(h, wakeups.notify, build)
    assert state.needs_tick and state.scheduler_live is True
    assert (await h.build(build))["tick_requested_at"] is None
    assert (await _svc(h, wakeups.read_notify, build)).needs_tick

    await _svc(h, wakeups.clear_notify, build)
    assert not (await _svc(h, wakeups.read_notify, build)).needs_tick
    await _svc(h, builds.cancel_build, build)
    state = await _svc(h, wakeups.notify, build)
    assert not state.needs_tick and not await _flagged(h, build)


async def test_wake_candidates_hand_each_build_out_once_per_window(h: Harness):
    """Flagged RUNNING reactive builds with no live lease, not handed out
    within the window, oldest flag first, at most 20; each is stamped, so a
    second caller gets nothing until the window passes."""
    t = item("T")
    built = []
    for _ in range(22):
        build, _ = await h.planned([t], [t])
        built.append(build)
    await _reactive(h, *built)
    for age, build in enumerate(reversed(built)):
        await _sql(
            h,
            "UPDATE build SET needs_tick_at = now() - make_interval(secs => :s)"
            " WHERE id = :b",
            s=age + 1,
            b=build,
        )
    leased, quiet, not_reactive = built[0], built[1], (await h.planned([t], [t]))[0]
    await _svc(h, wakeups.acquire_lease, leased, owner_id="o", ttl_seconds=60)
    await _svc(h, wakeups.clear_notify, quiet)
    await _sql(
        h, "UPDATE build SET needs_tick_at = now() WHERE id = :b", b=not_reactive
    )

    first = await _svc(h, wakeups.wake_candidates)
    assert [c.build_id for c in first] == built[2:22]
    assert {c.reactive_app_name for c in first} == {"app"}
    assert await _svc(h, wakeups.wake_candidates) == []

    await _sql(
        h,
        "UPDATE build SET tick_requested_at = now() - make_interval(secs => :w)",
        w=(wakeups.WAKE_HANDOUT_WINDOW + timedelta(seconds=1)).total_seconds(),
    )
    again = await _svc(h, wakeups.wake_candidates, limit=5)
    assert [c.build_id for c in again] == built[2:7]


# --------------------------------------------------------------------------
# The scheduler lease
# --------------------------------------------------------------------------


async def test_the_scheduler_lease_is_single_flight_and_owner_checked(h: Harness):
    t = item("T")
    build, _ = await h.planned([t], [t])
    first = await _svc(h, wakeups.acquire_lease, build, owner_id="a", ttl_seconds=60)
    assert first.held and first.expires_at is not None
    assert not (
        await _svc(h, wakeups.acquire_lease, build, owner_id="b", ttl_seconds=60)
    ).held
    again = await _svc(h, wakeups.acquire_lease, build, owner_id="a", ttl_seconds=60)
    assert again.held  # a retried acquire is not a lost race
    assert not (
        await _svc(h, wakeups.renew_lease, build, owner_id="b", ttl_seconds=60)
    ).held
    assert not (await _svc(h, wakeups.release_lease, build, owner_id="b")).held

    await _sql(
        h,
        "UPDATE build SET scheduler_lease_until = now() - interval '1 second'"
        " WHERE id = :b",
        b=build,
    )
    assert not (
        await _svc(h, wakeups.renew_lease, build, owner_id="a", ttl_seconds=60)
    ).held
    taken = await _svc(h, wakeups.acquire_lease, build, owner_id="b", ttl_seconds=60)
    assert taken.held
    assert not (await _svc(h, wakeups.release_lease, build, owner_id="a")).held
    assert (await _svc(h, wakeups.release_lease, build, owner_id="b")).held
    assert (await h.build(build))["scheduler_lease_owner"] is None


# --------------------------------------------------------------------------
# Reactive meta, tick summaries
# --------------------------------------------------------------------------


async def test_reactive_meta_is_an_upsert_and_reaches_the_frontier(h: Harness):
    t = item("T")
    build, _ = await h.planned([t], [t])
    await _svc(
        h, reactive.set_reactive_meta, build, app_name="app", tick_kwargs={"linger": 5}
    )
    bare = await _svc(
        h, reactive.set_reactive_meta, build, app_name="app2", tick_kwargs=None
    )
    assert bare.reactive_app_name == "app2"
    assert bare.reactive_tick_kwargs == {"linger": 5}
    frontier = await h.frontier(build)
    assert frontier.reactive_app_name == "app2"
    assert frontier.reactive_tick_kwargs == {"linger": 5}


async def test_tick_summaries_are_verbatim_and_pruned(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(app_settings, "max_tick_summaries_per_build", 3)
    t = item("T")
    build, _ = await h.planned([t], [t])
    for i in range(5):
        await _svc(
            h, reactive.add_tick_summary, build, {"outcome": f"o{i}", "novel": i}
        )
    rows = await _svc(h, reactive.list_tick_summaries, build)
    assert [r.outcome for r in rows] == ["o4", "o3", "o2"]
    assert rows[0].summary == {"outcome": "o4", "novel": 4}


async def test_tick_summary_retention_is_single_flight_per_build(
    h: Harness, async_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
):
    """Insert-and-prune runs under the build row lock: a summary written
    while another writer's insert is uncommitted waits for it, then prunes
    with it counted — rather than each pruning from a snapshot without the
    other's row and the trail keeping more than the cap."""
    monkeypatch.setattr(app_settings, "max_tick_summaries_per_build", 1)
    t = item("T")
    build, _ = await h.planned([t], [t])
    async with async_engine.connect() as other:
        await other.execute(
            text("SELECT 1 FROM build WHERE id = :b FOR NO KEY UPDATE"),
            {"b": build},
        )
        await other.execute(
            text(
                "INSERT INTO build_tick_summary (id, environment_id, build_id,"
                " outcome, summary, created_at) VALUES (:id, :env, :b, 'first',"
                " '{\"outcome\": \"first\"}', now() - interval '1 second')"
            ),
            {"id": uuid4(), "env": ENV, "b": build},
        )
        adding = asyncio.create_task(
            _svc(h, reactive.add_tick_summary, build, {"outcome": "second"})
        )
        await asyncio.sleep(0.3)
        assert not adding.done(), "the summary must wait for the build lock"
        await other.commit()
    await asyncio.wait_for(adding, 10)
    rows = await _svc(h, reactive.list_tick_summaries, build)
    assert [r.outcome for r in rows] == ["second"]


async def test_reactive_routes_over_http(client: AsyncClient, h: Harness):
    t = item("T")
    build, _ = await h.planned([t], [t])
    base = f"/api/v2/builds/{build}"
    meta = await client.put(f"{base}/reactive-meta", json={"app_name": "app"})
    assert meta.status_code == 200 and meta.json()["reactive_app_name"] == "app"

    # A caller that cannot spawn leaves the build to the drainers.
    await client.post(f"{base}/notify", params={"can_spawn": False})
    assert (await client.get(f"{base}/notify")).json()["needs_tick"]
    candidates = await client.post("/api/v2/builds/wake-candidates")
    assert candidates.json()["builds"] == [
        {"build_id": str(build), "reactive_app_name": "app"}
    ]
    await client.delete(f"{base}/notify")
    assert not (await client.get(f"{base}/notify")).json()["needs_tick"]
    notified = await client.post(f"{base}/notify")
    assert notified.json() == {
        "build_id": str(build),
        "needs_tick": True,
        "scheduler_live": False,
    }

    lease = await client.post(
        f"{base}/scheduler-lease", params={"owner_id": "o", "ttl_seconds": 30}
    )
    assert lease.json()["held"]
    renewed = await client.put(f"{base}/scheduler-lease", params={"owner_id": "o"})
    assert renewed.json()["held"]
    released = await client.delete(f"{base}/scheduler-lease", params={"owner_id": "o"})
    assert released.json()["held"]
    bad = await client.post(
        f"{base}/scheduler-lease", params={"owner_id": "o", "ttl_seconds": 1}
    )
    assert bad.status_code == 422

    summary = await client.post(
        f"{base}/tick-summaries", json={"outcome": "lingered_out", "ticks": 3}
    )
    assert summary.status_code == 201
    listed = await client.get(f"{base}/tick-summaries")
    assert listed.json()["summaries"][0]["summary"] == {
        "outcome": "lingered_out",
        "ticks": 3,
    }
