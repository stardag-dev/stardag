"""The frontier of a build's active plan (``api-pg`` tier).

Written from design.md, "The runnable rule" and "Registration" (closure is
kept as a mechanism), before the service. Each test names the scenario it
pins where the scenario table has one.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from stardag_api.models import BuildStatus
from stardag_api.services.errors import Conflict
from stardag_api.services.frontier import Frontier
from tests.v2_support import Harness, item, observed, task_ids, unexpanded


@pytest.fixture
def h(session_factory) -> Harness:
    return Harness(session_factory)


async def test_runnable_discovery_and_running_follow_the_predicates(h: Harness):
    """``discovery_job`` = not COMPLETED, not excluded, unexpanded;
    ``runnable`` = ACTIONABLE, not excluded, expanded, every upstream task
    COMPLETED; ``running`` = RUNNING with a live claim. ACTIONABLE includes
    RUNNING with a lapsed claim (S21), which is therefore runnable and not
    running."""
    leaf = item("Leaf")
    mid = item("Mid", upstreams=[leaf])
    root = item("Root", upstreams=[mid])
    build, plan = await h.planned([root])

    frontier = await h.frontier(build)
    assert frontier.plan_id == plan.id and not frontier.sealed
    assert task_ids(frontier.discovery_jobs) == {root.task_id}
    assert not frontier.runnable and not frontier.running

    await h.register(plan.id, [leaf, mid, root])
    frontier = await h.frontier(build)
    assert task_ids(frontier.runnable) == {leaf.task_id}
    assert not frontier.discovery_jobs
    assert frontier.runnable[0].body == leaf.body

    first = await h.start(plan.id, leaf)
    frontier = await h.frontier(build)
    assert task_ids(frontier.running) == {leaf.task_id}
    assert not frontier.runnable

    await h.lapse_claim(leaf)
    frontier = await h.frontier(build)
    assert task_ids(frontier.runnable) == {leaf.task_id}
    assert not frontier.running

    second = await h.start(plan.id, leaf)
    assert second != first
    from stardag_api.services.transitions import Transition

    await h.transition(plan.id, leaf, Transition.complete(second))
    frontier = await h.frontier(build)
    assert task_ids(frontier.runnable) == {mid.task_id}


async def test_plan_complete_needs_the_seal_and_every_member_completed(h: Harness):
    """``plan_complete`` (diagnostic) = sealed, and every non-excluded
    member COMPLETED: an unsealed plan is a request not yet fully stated."""
    root = item("Root")
    build, plan = await h.planned([root], [root])
    await h.run(plan.id, root)
    assert not (await h.frontier(build)).plan_complete
    await h.seal(plan.id)
    assert (await h.frontier(build)).plan_complete


async def test_s23_closure_admits_upstreams_another_plan_expanded(h: Harness):
    """S23 — B admitted X unexpanded (pruned-complete); X is invalidated
    and A expands it first. B's closure step admits X's new upstream U, B's
    discovery job for X is skipped (the shared ``expanded_at`` is set), and
    X is gated in B on a member B holds (closure + shared flag)."""
    deployment = await h.new_deployment()
    u = item("U")
    x = item("X", upstreams=[u])
    rb = item("RB", upstreams=[x])
    build_b, plan_b = await h.planned(
        [rb], [observed(unexpanded(x), True), rb], deployment_id=deployment
    )
    # A's discovery finds X's target missing and expands it: X -> U.
    ra = item("RA", upstreams=[x])
    await h.planned([ra], [u, observed(x, False), ra], deployment_id=deployment)
    assert (await h.task(x))["status"] == "pending"

    frontier = await h.frontier(build_b)
    assert frontier.closure is not None and frontier.closure.admitted == 1
    members = await h.members(plan_b.id)
    assert members[u.task_id]["admitted_by"] == "closure"
    assert x.task_id not in task_ids(frontier.discovery_jobs)
    assert x.task_id not in task_ids(frontier.runnable)
    assert u.task_id in task_ids(frontier.runnable)


async def test_closure_conflict_fails_the_build_naming_both_members(h: Harness):
    """A conflict found by the closure step (the plan holds U1, an edge
    now reaches U2 of the same completion) fails the build naming both
    instances: ``BUILD_FAILED`` is recorded and the frontier reports it."""
    deployment = await h.new_deployment()
    u1 = item("U", extra={"mode": "fast"})
    u2 = item("U", extra={"mode": "slow"})
    x = item("X", upstreams=[u2])
    build_a, plan_a = await h.planned(
        [item("RA", upstreams=[x])],
        [u1, observed(unexpanded(x), True)],
        deployment_id=deployment,
    )
    await h.planned([item("RB", upstreams=[x])], [u2, x], deployment_id=deployment)

    frontier = await h.frontier(build_a)
    assert frontier.closure is not None and frontier.closure.build_failed
    (conflict,) = frontier.closure.conflicts
    assert conflict.task_id == u1.task_id
    assert conflict.fields == ["mode"]
    assert (await h.build(build_a))["status"] == "failed"
    (failed,) = await h.events(build_id=build_a, types=["build_failed"])
    for instance in (conflict.member_instance_id, conflict.other_instance_id):
        assert str(instance) in failed["error_message"]
    assert plan_a.id  # the plan stays; the build failed


async def _closure_conflict_setup(h: Harness):
    """Plan A holds U1; plan B, in A's scope, expands X (a member of A)
    with an edge to U2 of the same completion and to a fresh V. A's closure
    step then admits V and finds U1/U2 in conflict."""
    deployment = await h.new_deployment()
    u1 = item("U", extra={"mode": "fast"})
    u2 = item("U", extra={"mode": "slow"})
    v = item("V")
    x = item("X", upstreams=[u2, v])
    build_a, plan_a = await h.planned(
        [item("RA", upstreams=[x])],
        [u1, observed(unexpanded(x), True)],
        deployment_id=deployment,
    )
    await h.planned([item("RB", upstreams=[x])], [u2, v, x], deployment_id=deployment)
    return build_a, plan_a, v


@pytest.mark.parametrize("via", ["closure", "frontier"])
async def test_closure_locks_the_build_before_it_admits(
    h: Harness, async_engine: AsyncEngine, via: str
):
    """Lock order build → plan → task rows holds for the closure step too:
    it takes the build row ``FOR NO KEY UPDATE`` (as plan creation and
    sealing do) before reading or admitting anything. Admitting first and
    locking the build only to fail it over a conflict would hold
    ``plan_member`` inserts while waiting on a plan retry that holds the
    build lock and waits on those inserts: a deadlock.

    Pinned two ways: another session holding the build lock (a plan retry
    in flight) stops the closure before any admission — it waits, then
    finishes, admitting V and failing the build; and the build lock is the
    closure's first locking or writing statement."""
    build_a, plan_a, v = await _closure_conflict_setup(h)

    statements: list[str] = []

    def capture(conn, cursor, statement, *args):  # noqa: ARG001
        statements.append(statement)

    async with async_engine.connect() as retry:
        await retry.execute(
            text("SELECT 1 FROM build WHERE id = :b FOR NO KEY UPDATE"),
            {"b": build_a},
        )
        event.listen(async_engine.sync_engine, "before_cursor_execute", capture)
        try:
            call = h.closure(plan_a.id) if via == "closure" else h.frontier(build_a)
            closing = asyncio.create_task(call)
            await asyncio.sleep(0.3)
            assert not closing.done(), "the closure must wait for the build lock"
            assert not [
                s
                for s in statements
                if s.lstrip().upper().startswith(("INSERT", "UPDATE"))
            ], "nothing may be admitted before the build lock is held"
            await retry.commit()
            result = await asyncio.wait_for(closing, 10)
        finally:
            event.remove(async_engine.sync_engine, "before_cursor_execute", capture)

    closure = result.closure if isinstance(result, Frontier) else result
    assert closure is not None and closure.build_failed
    assert closure.admitted == 1
    assert (await h.members(plan_a.id))[v.task_id]["admitted_by"] == "closure"
    assert (await h.build(build_a))["status"] == "failed"

    ordered = [
        s
        for s in statements
        if re.search(r"\bFOR (NO KEY UPDATE|SHARE|KEY SHARE|UPDATE)\b", s)
        or s.lstrip().upper().startswith(("INSERT", "UPDATE"))
    ]
    assert re.search(r"\bFROM build\b", ordered[0]), ordered[0]
    assert "FOR NO KEY UPDATE" in ordered[0]
    assert not [s for s in statements if re.search(r"\bFOR UPDATE\b", s)]


async def test_closure_and_a_plan_retry_do_not_deadlock(h: Harness):
    """Two sessions: a plan retry (``create_plan`` re-sent for the same
    scope) and a closure step that finds a conflict, interleaved many times
    over one build. Both lock the build row first, so they serialise:
    every call returns, and the closure's verdict stands."""
    build_a, plan_a, _ = await _closure_conflict_setup(h)
    roots = [item("RA", upstreams=[])]  # same instance: upstreams are not body

    async def retry():
        return await h.plan(build_a, plan_a.deployment_id, roots, plan_id=plan_a.id)

    outcomes = await asyncio.wait_for(
        asyncio.gather(
            *[f() for _ in range(5) for f in (retry, lambda: h.closure(plan_a.id))],
            return_exceptions=True,
        ),
        30,
    )
    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
    assert (await h.build(build_a))["status"] == "failed"


async def test_a_build_that_is_not_running_hands_out_no_work(h: Harness):
    """A closure conflict fails the build in the frontier call itself: that
    call, and every later one, returns no runnable members and no discovery
    jobs and reports ``build_status``; a claiming start on a member the
    failed build's plan still holds is 409 ``build_not_running``."""
    deployment = await h.new_deployment()
    u1 = item("U", extra={"mode": "fast"})
    u2 = item("U", extra={"mode": "slow"})
    x = item("X", upstreams=[u2])
    ready = item("Ready")
    build_a, plan_a = await h.planned(
        [item("RA", upstreams=[x, ready])],
        [u1, ready, observed(unexpanded(x), True)],
        deployment_id=deployment,
    )
    assert ready.task_id in task_ids((await h.frontier(build_a)).runnable)
    await h.planned([item("RB", upstreams=[x])], [u2, x], deployment_id=deployment)

    for _ in range(2):
        frontier = await h.frontier(build_a)
        assert frontier.build_status == BuildStatus.FAILED
        assert frontier.runnable == [] and frontier.discovery_jobs == []
    with pytest.raises(Conflict) as exc:
        await h.start(plan_a.id, ready)
    assert exc.value.code == "build_not_running"
    assert exc.value.detail["build_status"] == "failed"
    assert (await h.task(ready))["status"] == "pending"
