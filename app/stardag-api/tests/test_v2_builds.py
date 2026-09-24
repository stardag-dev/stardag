"""Build lifecycle (``api-pg`` tier).

Written from design.md, "The runnable rule" (build status, ``force``,
releasing claims, resumability), "Registration" (lifecycle transitions are
idempotent by state) and the ``build`` entity (the delete guard). Each test
names its scenario and the rule that decides it.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from stardag_api.services import builds
from stardag_api.services.errors import BadRequest, Conflict
from stardag_api.services.transitions import Transition
from tests.v2_support import (
    ENV,
    Harness,
    item,
    observed,
    task_ids,
    unexpanded,
)


@pytest.fixture
def h(session_factory) -> Harness:
    return Harness(session_factory)


async def _call(h: Harness, fn: Any, build_id: UUID, **kwargs: Any) -> Any:
    async with h.sf() as s:
        return await fn(s, ENV, build_id, **kwargs)


async def _build_events(h: Harness, build_id: UUID) -> list[str]:
    return [
        e["type"]
        for e in await h.events(build_id=build_id)
        if e["type"].startswith("build_")
    ]


async def _exclude(h: Harness, plan_id: UUID, it: Any) -> None:
    async with h.sf() as s:
        await s.execute(
            text(
                "UPDATE plan_member SET excluded_at = now(), excluded_reason ="
                " 'operator' WHERE plan_id = :p AND task_pk ="
                " (SELECT id FROM task WHERE task_id = :t)"
            ),
            {"p": plan_id, "t": it.task_id},
        )
        await s.commit()


async def test_a_plans_roots_must_be_the_builds_request(h: Harness):
    """``POST /builds`` records ``root_task_ids``; a plan whose roots name
    other task ids is 400 ``root_mismatch`` (a build is one request)."""
    root, other = item("Root"), item("Other")
    deployment = await h.new_deployment()
    build = await h.new_build([root])
    with pytest.raises(BadRequest) as exc:
        await h.plan(build, deployment, [other])
    assert exc.value.code == "root_mismatch"
    assert exc.value.detail == {
        "unexpected": [other.task_id],
        "missing": [root.task_id],
    }


async def test_complete_recomputes_plan_complete(h: Harness):
    """``/complete`` is refused (409 ``plan_incomplete``) unless the active
    plan is sealed and every non-excluded member COMPLETED; a re-delivered
    complete finds the state and writes nothing."""
    leaf = item("Leaf")
    root = item("Root", upstreams=[leaf])
    build, plan = await h.planned([root], [leaf, root])

    with pytest.raises(Conflict) as exc:
        await _call(h, builds.complete_build, build)
    assert (exc.value.code, exc.value.detail["reason"]) == (
        "plan_incomplete",
        "not_sealed",
    )
    await h.seal(plan.id)
    await h.run(plan.id, leaf)
    with pytest.raises(Conflict) as exc:
        await _call(h, builds.complete_build, build)
    assert exc.value.detail["reason"] == "members_incomplete"
    assert exc.value.detail["task_ids"] == [root.task_id]

    await h.run(plan.id, root)
    done = await _call(h, builds.complete_build, build)
    assert done.status == "completed" and done.completed_at is not None
    again = await _call(h, builds.complete_build, build)
    assert again.completed_at == done.completed_at
    assert await _build_events(h, build) == ["build_completed"]


async def test_force_overrides_members_never_a_missing_seal(h: Harness):
    """``force`` is the operator override for outstanding members; an
    unsealed plan is a request not yet fully stated, so it is refused even
    with ``force``."""
    leaf = item("Leaf")
    root = item("Root", upstreams=[leaf])
    build, plan = await h.planned([root], [leaf, root])
    with pytest.raises(Conflict) as exc:
        await _call(h, builds.complete_build, build, force=True)
    assert exc.value.detail["reason"] == "not_sealed"

    await h.seal(plan.id)
    forced = await _call(h, builds.complete_build, build, force=True)
    assert forced.status == "completed"
    (event,) = await h.events(build_id=build, types=["build_completed"])
    assert event["event_metadata"] == {"force": True, "outstanding": 2}


async def test_s18_an_excluded_root_is_never_forced_complete(h: Harness):
    """S18 (root) — an excluded root is a request that cannot be met:
    ``/complete`` is refused even with ``force``; the way out is ``fail`` or
    ``cancel``. (The exclusion cascade that fails the build on its own is
    step 3b; the exclusion is written directly here.)"""
    root = item("Root")
    build, plan = await h.planned([root], [root], seal=True)
    await _exclude(h, plan.id, root)
    with pytest.raises(Conflict) as exc:
        await _call(h, builds.complete_build, build, force=True)
    assert (exc.value.code, exc.value.detail["reason"]) == (
        "plan_incomplete",
        "root_excluded",
    )
    failed = await _call(h, builds.fail_build, build, error_message="root excluded")
    assert failed.status == "failed"


async def test_an_excluded_member_does_not_gate_completion(h: Harness):
    leaf = item("Leaf")
    root = item("Root", upstreams=[leaf])
    extra = item("Extra")
    # Registered before the seal: a sealed plan takes no new member.
    build, plan = await h.planned([root], [leaf, root, extra], seal=True)
    await _exclude(h, plan.id, extra)
    await h.run(plan.id, leaf)
    await h.run(plan.id, root)
    assert (await _call(h, builds.complete_build, build)).status == "completed"


@pytest.mark.parametrize("terminal", ["cancel", "fail", "complete"])
async def test_terminal_transitions_release_the_builds_claims(
    h: Harness, terminal: str
):
    """``complete``, ``fail`` and ``cancel`` release every claim held by
    any of the build's plans: ``claim_outcome = released``, the task
    CANCELLED (the build stopped wanting it — not a result, so ACTIONABLE
    for every other build), ``ended_at`` untouched (the worker may still
    run). Its later report is late: recorded and refused. Another build
    holding the task can then claim it."""
    deployment = await h.new_deployment()
    t = item("T")
    build_a, plan_a = await h.planned([t], [t], deployment_id=deployment, seal=True)
    build_b, plan_b = await h.planned([t], [t], deployment_id=deployment)
    execution = await h.start(plan_a.id, t)

    if terminal == "cancel":
        await _call(h, builds.cancel_build, build_a)
    elif terminal == "fail":
        await _call(h, builds.fail_build, build_a)
    else:
        await _call(h, builds.complete_build, build_a, force=True)

    task = await h.task(t)
    assert task["status"] == "cancelled" and task["claim_plan_id"] is None
    ledger = await h.execution(execution)
    assert ledger["claim_outcome"] == "released" and ledger["ended_at"] is None
    (cancelled,) = await h.events(t, types=["task_cancelled"])
    assert cancelled["execution_id"] == execution and cancelled["plan_id"] == plan_a.id

    with pytest.raises(Conflict) as exc:
        await h.transition(plan_a.id, t, Transition.complete(execution))
    assert exc.value.code == "execution_not_current"
    assert (await h.task(t))["status"] == "cancelled"

    assert t.task_id in task_ids((await h.frontier(build_b)).runnable)
    await h.start(plan_b.id, t)


async def test_a_superseded_plans_claims_are_released_too(h: Harness):
    """The claims of *every* plan of the build, not only the active one."""
    deployment = await h.new_deployment()
    t = item("T")
    root = item("Root", upstreams=[t])
    build = await h.new_build([root])
    p1 = await h.plan(build, deployment, [root])
    await h.register(p1.id, [t, root])
    await h.seal(p1.id)
    execution = await h.start(p1.id, t)
    p2 = await h.plan(build, deployment, [root], settings={"S": "2"})
    await h.register(p2.id, [t, root])
    await h.seal(p2.id)

    await _call(h, builds.cancel_build, build)
    assert (await h.execution(execution))["claim_outcome"] == "released"


async def test_exit_early_releases_nothing(h: Harness):
    t = item("T")
    build, plan = await h.planned([t], [t])
    execution = await h.start(plan.id, t)
    exited = await _call(h, builds.exit_early, build)
    assert exited.status == "exit_early"
    task = await h.task(t)
    assert task["status"] == "running" and task["execution_id"] == execution
    assert (await h.execution(execution))["claim_released_at"] is None


async def test_lifecycle_transitions_are_idempotent_by_state(h: Harness):
    """A re-delivered transition finds the build in the requested state,
    returns it, and writes no event and no timestamp."""
    t = item("T")
    build, _ = await h.planned([t], [t])
    for fn in (builds.fail_build, builds.cancel_build, builds.exit_early):
        first = await _call(h, fn, build)
        again = await _call(h, fn, build)
        assert again.status == first.status
        assert again.completed_at == first.completed_at
    assert await _build_events(h, build) == [
        "build_failed",
        "build_cancelled",
        "build_exit_early",
    ]


async def test_resume_makes_the_build_running_and_is_idempotent(h: Harness):
    t = item("T")
    build, plan = await h.planned([t], [t])
    await _call(h, builds.cancel_build, build)
    resumed = await _call(h, builds.resume_build, build)
    assert resumed.changed and resumed.plan is None
    assert resumed.build.status == "running" and resumed.build.is_resumed
    assert resumed.build.completed_at is None
    again = await _call(h, builds.resume_build, build)
    assert not again.changed
    assert await _build_events(h, build) == ["build_cancelled", "build_resumed"]


async def test_s14_resume_reuses_or_reactivates_the_plan_for_the_scope(h: Harness):
    """Resume under a scope whose plan was active before reactivates it
    (the active one superseded, in one transaction); under the active
    plan's scope it reuses it; under a scope with no plan it returns none,
    and the driver runs discovery and creates one."""
    deployment = await h.new_deployment()
    root = item("Root")
    build = await h.new_build([root])
    p1 = await h.plan(build, deployment, [root])
    await h.register(p1.id, [root])
    await h.seal(p1.id)
    p2 = await h.plan(build, deployment, [root], settings={"S": "2"})
    await h.register(p2.id, [root])
    await h.seal(p2.id)
    assert (await h.frontier(build)).plan_id == p2.id

    back = await _call(h, builds.resume_build, build, deployment_id=deployment)
    assert back.changed and back.plan is not None and back.plan.id == p1.id
    assert back.plan.activated_at is not None and back.plan.superseded_at is None
    assert (await h.frontier(build)).plan_id == p1.id

    same = await _call(h, builds.resume_build, build, deployment_id=deployment)
    assert not same.changed and same.plan is not None and same.plan.id == p1.id

    fresh = await _call(
        h, builds.resume_build, build, deployment_id=deployment, settings={"S": "3"}
    )
    assert fresh.plan is None and not fresh.changed


async def test_resume_does_not_reactivate_an_old_deployment(h: Harness):
    """Reactivation makes the seal's deployment check: rollover only moves
    forward, so a plan under a deployment that is no longer current is not
    brought back (409 ``deployment_not_current``)."""
    old = await h.new_deployment(app_name="svc")
    root = item("Root")
    build = await h.new_build([root])
    p1 = await h.plan(build, old, [root])
    await h.register(p1.id, [root])
    await h.seal(p1.id)
    new = await h.new_deployment(app_name="svc")
    p2 = await h.plan(build, new, [root])
    await h.register(p2.id, [root])
    await h.seal(p2.id)
    with pytest.raises(Conflict) as exc:
        await _call(h, builds.resume_build, build, deployment_id=old)
    assert exc.value.code == "deployment_not_current"


async def test_s17_delete_is_refused_while_work_is_live(h: Harness):
    """S17 — deleting a build is refused (409 ``build_has_live_work``) while
    a plan of it holds a live claim, or an execution of it has not reported
    its end — also after a release, when the worker may still run; once
    every execution has ended, plans, members and executions cascade and
    events keep their rows with the pointers NULL."""
    t = item("T")
    build, plan = await h.planned([t], [t], seal=True)
    execution = await h.start(plan.id, t)
    with pytest.raises(Conflict) as exc:
        await _call(h, builds.delete_build, build)
    assert exc.value.code == "build_has_live_work"
    assert exc.value.detail["live_claim"]

    await h.lapse_claim(t)
    with pytest.raises(Conflict) as exc:
        await _call(h, builds.delete_build, build)
    assert exc.value.detail == {
        "build_id": str(build),
        "live_claim": False,
        "unended_executions": True,
    }

    await _call(h, builds.cancel_build, build)
    with pytest.raises(Conflict):
        await _call(h, builds.delete_build, build)
    with pytest.raises(Conflict):  # late: ends the ledger, task unchanged
        await h.transition(plan.id, t, Transition.complete(execution))

    await _call(h, builds.delete_build, build)
    assert await h.count("plan") == 0 and await h.count("execution") == 0
    events = await h.events(t)
    assert events and all(e["build_id"] is None for e in events)
    assert (await h.task(t))["execution_id"] is None


async def test_build_lifecycle_over_http(client: AsyncClient, h: Harness):
    deployment = await h.new_deployment()
    root = item("Root")
    build = (
        await client.post("/api/v2/builds", json={"root_task_ids": [root.task_id]})
    ).json()
    base = f"/api/v2/builds/{build['id']}"
    plan = (
        await client.post(
            f"{base}/plans",
            json={
                "plan_id": str(UUID(int=7)),
                "deployment_id": str(deployment),
                "roots": [unexpanded(root).model_dump(mode="json")],
            },
        )
    ).json()
    refused = await client.post(f"{base}/complete", json={"force": True})
    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "not_sealed"

    cancelled = await client.post(f"{base}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    resumed = await client.post(
        f"{base}/resume", json={"deployment_id": str(deployment)}
    )
    assert resumed.json()["build"]["status"] == "running"
    assert resumed.json()["plan"]["id"] == plan["id"]
    exited = await client.post(f"{base}/exit-early")
    assert exited.json()["status"] == "exit_early"
    failed = await client.post(f"{base}/fail", json={"error_message": "x"})
    assert failed.json()["status"] == "failed"
    deleted = await client.delete(base)
    assert deleted.status_code == 204
    assert (await client.get(base)).status_code == 404

    missing = await client.post("/api/v2/builds", json={"root_task_ids": []})
    assert missing.status_code == 422


async def test_resume_does_not_reactivate_under_a_pending_replacement(h: Harness):
    """Reactivation respects the seal's "no higher generation" rule: while
    a later request for the build is registering (created, never
    activated), a resume does not bring an older plan back — 409
    ``plan_superseded``, the latest request wins. Once that replacement is
    sealed, moving back to a recorded request is what a resume is for."""
    deployment = await h.new_deployment()
    root = item("Root")
    build = await h.new_build([root])
    p1 = await h.plan(build, deployment, [root])
    await h.register(p1.id, [root])
    await h.seal(p1.id)
    p2 = await h.plan(build, deployment, [root], settings={"S": "2"})
    await h.register(p2.id, [root])
    await h.seal(p2.id)
    p3 = await h.plan(build, deployment, [root], settings={"S": "3"})

    with pytest.raises(Conflict) as exc:
        await _call(h, builds.resume_build, build, deployment_id=deployment)
    assert exc.value.code == "plan_superseded"
    assert (await h.frontier(build)).plan_id == p2.id

    await h.register(p3.id, [root])
    await h.seal(p3.id)
    back = await _call(h, builds.resume_build, build, deployment_id=deployment)
    assert back.plan is not None and back.plan.id == p1.id


async def test_a_local_deployment_is_never_superseded(h: Harness):
    """A local deployment is authoritative for its own plans: a newer local
    deployment (another commit) does not make an older one's plan
    unsealable or unresumable — the currency checks are for Modal only."""
    older = await h.new_deployment(kind="local", app_name="local")
    await h.new_deployment(kind="local", app_name="local")
    root = item("Root")
    build = await h.new_build([root])
    p1 = await h.plan(build, older, [root])
    await h.register(p1.id, [root])
    assert (await h.seal(p1.id)).sealed_at is not None
    p2 = await h.plan(build, older, [root], settings={"S": "2"})
    await h.register(p2.id, [root])
    await h.seal(p2.id)
    back = await _call(h, builds.resume_build, build, deployment_id=older)
    assert back.plan is not None and back.plan.id == p1.id


# --------------------------------------------------------------------------
# /complete runs the closure step
# --------------------------------------------------------------------------


async def test_complete_admits_what_another_plan_made_reachable_since_the_seal(
    h: Harness,
):
    """``/complete`` runs the closure step before it recomputes the
    predicate: a plan sealed holding X (COMPLETED, unexpanded), then
    another plan in its scope expands X with an edge to a pending U. U is
    now reachable and must gate completion rather than be missed."""
    deployment = await h.new_deployment()
    u = item("U")
    x = item("X", upstreams=[u])
    build, plan = await h.planned(
        [x], [observed(unexpanded(x), True)], deployment_id=deployment, seal=True
    )
    await h.planned(
        [item("RB", upstreams=[x])], [u, observed(x, True)], deployment_id=deployment
    )

    with pytest.raises(Conflict) as exc:
        await _call(h, builds.complete_build, build)
    assert (exc.value.code, exc.value.detail["reason"]) == (
        "plan_incomplete",
        "members_incomplete",
    )
    assert exc.value.detail["task_ids"] == [u.task_id]
    assert (await h.build(build))["status"] == "running"
    # The refusal rolls its admission back; the frontier's closure step
    # admits U for the worker that runs it.
    assert u.task_id in task_ids((await h.frontier(build)).runnable)
    await h.run(plan.id, u)
    assert (await _call(h, builds.complete_build, build)).status == "completed"


async def test_complete_fails_the_build_on_a_closure_conflict(h: Harness):
    """A conflict the closure step of ``/complete`` finds fails the build
    (committed) and refuses completion with ``instance_conflict``, as on
    ``/seal``."""
    deployment = await h.new_deployment()
    u1 = item("U", extra={"mode": "fast"})
    u2 = item("U", extra={"mode": "slow"})
    x = item("X", upstreams=[u2])
    root = item("RA", upstreams=[item("U", extra={"mode": "fast"}), x])
    build, plan = await h.planned(
        [root], [u1, observed(unexpanded(x), True), root], deployment_id=deployment
    )
    await h.run(plan.id, u1)
    await h.run(plan.id, root)
    await h.seal(plan.id)
    await h.planned(
        [item("RB", upstreams=[x])], [u2, observed(x, True)], deployment_id=deployment
    )

    with pytest.raises(Conflict) as exc:
        await _call(h, builds.complete_build, build)
    assert exc.value.code == "instance_conflict"
    assert (await h.build(build))["status"] == "failed"


# --------------------------------------------------------------------------
# Claims synchronise on the build row
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ending", ["cancel", "delete"])
async def test_a_claim_waits_for_a_terminal_transition_or_delete_in_flight(
    h: Harness, async_engine: AsyncEngine, ending: str
):
    """A claiming start takes the build row ``FOR SHARE`` before its task
    row, so a terminal transition or a delete holding the build ``FOR NO
    KEY UPDATE`` — its release loop or its live-work guard already run —
    is waited for, and the start then reads the new status and is refused
    ``build_not_running``. Without it the start would read RUNNING and
    commit a claim the build's end never saw: a finished build holding a
    live claim, or a delete cascading a just-created execution away."""
    root = item("Root")
    build, plan = await h.planned([root], [root], seal=True)

    async with async_engine.connect() as ender:
        await ender.execute(
            text("SELECT 1 FROM build WHERE id = :b FOR NO KEY UPDATE"),
            {"b": build},
        )
        starting = asyncio.create_task(h.start(plan.id, root))
        await asyncio.sleep(0.3)
        assert not starting.done(), "the claim must wait for the build's end"
        if ending == "cancel":
            await ender.execute(
                text("UPDATE build SET status = 'cancelled' WHERE id = :b"),
                {"b": build},
            )
        else:
            await ender.execute(text("DELETE FROM build WHERE id = :b"), {"b": build})
        await ender.commit()

    with pytest.raises(Conflict) as exc:
        await asyncio.wait_for(starting, 10)
    assert exc.value.code == "build_not_running"
    assert await h.count("execution") == 0
    assert (await h.task(root))["status"] == "pending"


async def test_claims_share_the_build_lock(h: Harness, async_engine: AsyncEngine):
    """Claims do not serialise with each other, nor with member chunks:
    another transaction holding the build ``FOR SHARE`` does not block a
    claiming start."""
    root = item("Root")
    build, plan = await h.planned([root], [root], seal=True)
    async with async_engine.connect() as other:
        await other.execute(
            text("SELECT 1 FROM build WHERE id = :b FOR SHARE"), {"b": build}
        )
        await asyncio.wait_for(h.start(plan.id, root, uuid4()), 5)
        await other.rollback()
    assert (await h.task(root))["status"] == "running"
